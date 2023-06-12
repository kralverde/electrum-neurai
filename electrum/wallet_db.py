#!/usr/bin/env python
#
# Electrum - lightweight Bitcoin client
# Copyright (C) 2015 Thomas Voegtlin
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
import os
import ast
import datetime
import json
import copy
import threading
from collections import defaultdict
from typing import Dict, Optional, List, Tuple, Set, Iterable, NamedTuple, Sequence, TYPE_CHECKING, Union
import binascii
import time

import attr

from . import util, bitcoin
from .asset import AssetMetadata, get_error_for_asset_name
from .util import profiler, WalletFileException, multisig_type, TxMinedInfo, bfh
from .invoices import Invoice, Request
from .keystore import bip44_derivation
from .transaction import Transaction, TxOutpoint, tx_from_any, PartialTransaction, PartialTxOutput
from .logging import Logger
from .lnutil import LOCAL, REMOTE, FeeUpdate, UpdateAddHtlc, LocalConfig, RemoteConfig, ChannelType
from .lnutil import ImportedChannelBackupStorage, OnchainChannelBackupStorage
from .lnutil import ChannelConstraints, Outpoint, ShachainElement
from .json_db import StoredDict, JsonDB, locked, modifier, StoredObject
from .plugin import run_hook, plugin_loaders
from .submarine_swaps import SwapData
from .version import ELECTRUM_VERSION

if TYPE_CHECKING:
    from .storage import WalletStorage


# seed_version is now used for the version of the wallet file

FINAL_SEED_VERSION = 1

class TxFeesValue(NamedTuple):
    fee: Optional[int] = None
    is_calculated_by_us: bool = False
    num_inputs: Optional[int] = None


@attr.s
class DBMetadata(StoredObject):
    creation_timestamp = attr.ib(default=None, type=int)
    first_electrum_version_used = attr.ib(default=None, type=str)

    def to_str(self) -> str:
        ts = self.creation_timestamp
        ver = self.first_electrum_version_used
        if ts is None or ver is None:
            return "unknown"
        date_str = datetime.date.fromtimestamp(ts).isoformat()
        return f"using {ver}, on {date_str}"


# note: subclassing WalletFileException for some specific cases
#       allows the crash reporter to distinguish them and open
#       separate tracking issues
class WalletFileExceptionVersion51(WalletFileException): pass


class WalletDB(JsonDB):

    def __init__(self, raw, *, manual_upgrades: bool):
        JsonDB.__init__(self, {})
        self._manual_upgrades = manual_upgrades
        self._called_after_upgrade_tasks = False
        if raw:  # loading existing db
            self.load_data(raw)
            self.load_plugins()
        else:  # creating new db
            self.put('seed_version', FINAL_SEED_VERSION)
            self._add_db_creation_metadata()
            self._after_upgrade_tasks()

    def load_data(self, s):
        try:
            self.data = json.loads(s)
        except Exception:
            try:
                d = ast.literal_eval(s)
                labels = d.get('labels', {})
            except Exception as e:
                raise WalletFileException("Cannot read wallet file. (parsing failed)")
            self.data = {}
            for key, value in d.items():
                try:
                    json.dumps(key)
                    json.dumps(value)
                except Exception:
                    self.logger.info(f'Failed to convert label to json format: {key}')
                    continue
                self.data[key] = value
        if not isinstance(self.data, dict):
            raise WalletFileException("Malformed wallet file (not dict)")

        if not self._manual_upgrades and self.requires_split():
            raise WalletFileException("This wallet has multiple accounts and must be split")

        if not self.requires_upgrade():
            self._after_upgrade_tasks()
        elif not self._manual_upgrades:
            self.upgrade()

    def requires_split(self):
        d = self.get('accounts', {})
        return len(d) > 1

    def get_split_accounts(self):
        result = []
        # backward compatibility with old wallets
        d = self.get('accounts', {})
        if len(d) < 2:
            return
        wallet_type = self.get('wallet_type')
        if wallet_type == 'old':
            assert len(d) == 2
            data1 = copy.deepcopy(self.data)
            data1['accounts'] = {'0': d['0']}
            data1['suffix'] = 'deterministic'
            data2 = copy.deepcopy(self.data)
            data2['accounts'] = {'/x': d['/x']}
            data2['seed'] = None
            data2['seed_version'] = None
            data2['master_public_key'] = None
            data2['wallet_type'] = 'imported'
            data2['suffix'] = 'imported'
            result = [data1, data2]

        # note: do not add new hardware types here, this code is for converting legacy wallets
        elif wallet_type in ['bip44', 'trezor', 'keepkey', 'ledger', 'btchip']:
            mpk = self.get('master_public_keys')
            for k in d.keys():
                i = int(k)
                x = d[k]
                if x.get("pending"):
                    continue
                xpub = mpk["x/%d'"%i]
                new_data = copy.deepcopy(self.data)
                # save account, derivation and xpub at index 0
                new_data['accounts'] = {'0': x}
                new_data['master_public_keys'] = {"x/0'": xpub}
                new_data['derivation'] = bip44_derivation(k)
                new_data['suffix'] = k
                result.append(new_data)
        else:
            raise WalletFileException("This wallet has multiple accounts and must be split")
        return result

    def requires_upgrade(self):
        return self.get_seed_version() < FINAL_SEED_VERSION

    @profiler
    def upgrade(self):
        self.logger.info('upgrading wallet format')
        if self._called_after_upgrade_tasks:
            # we need strict ordering between upgrade() and after_upgrade_tasks()
            raise Exception("'after_upgrade_tasks' must NOT be called before 'upgrade'")
        
        # Upgrades go here
        
        self.put('seed_version', FINAL_SEED_VERSION)  # just to be sure

        self._after_upgrade_tasks()

    def _after_upgrade_tasks(self):
        self._called_after_upgrade_tasks = True
        self._load_transactions()

    def _is_upgrade_method_needed(self, min_version, max_version):
        assert min_version <= max_version
        cur_version = self.get_seed_version()
        if cur_version > max_version:
            return False
        elif cur_version < min_version:
            raise WalletFileException(
                'storage upgrade: unexpected version {} (should be {}-{})'
                .format(cur_version, min_version, max_version))
        else:
            return True

    @locked
    def get_seed_version(self):
        seed_version = self.get('seed_version')
        if seed_version > FINAL_SEED_VERSION:
            raise WalletFileException('This version of Electrum is too old to open this wallet.\n'
                                      '(highest supported storage version: {}, version of this file: {})'
                                      .format(FINAL_SEED_VERSION, seed_version))
        return seed_version

    def _raise_unsupported_version(self, seed_version):
        msg = f"Your wallet has an unsupported seed version: {seed_version}."
        # generic exception
        raise WalletFileException(msg)

    def _add_db_creation_metadata(self):
        # store this for debugging purposes
        v = DBMetadata(
            creation_timestamp=int(time.time()),
            first_electrum_version_used=ELECTRUM_VERSION,
        )
        assert self.get("db_metadata", None) is None
        self.put("db_metadata", v)

    def get_db_metadata(self) -> Optional[DBMetadata]:
        # field only present for wallet files created with ver 4.4.0 or later
        return self.get("db_metadata")

    @locked
    def get_txi_addresses(self, tx_hash: str) -> List[str]:
        """Returns list of is_mine addresses that appear as inputs in tx."""
        assert isinstance(tx_hash, str)
        return list(self.txi.get(tx_hash, {}).keys())

    @locked
    def get_txo_addresses(self, tx_hash: str) -> List[str]:
        """Returns list of is_mine addresses that appear as outputs in tx."""
        assert isinstance(tx_hash, str)
        return list(self.txo.get(tx_hash, {}).keys())

    @locked
    def get_txi_addr(self, tx_hash: str, address: str) -> Iterable[Tuple[str, int, Optional[str]]]:
        """Returns an iterable of (prev_outpoint, value, asset)."""
        assert isinstance(tx_hash, str)
        assert isinstance(address, str)
        d = self.txi.get(tx_hash, {}).get(address, {})
        return list(((n, v, asset) for n, (v, asset) in d.items()))

    @locked
    def get_txo_addr(self, tx_hash: str, address: str) -> Dict[int, Tuple[int, Optional[str], bool]]:
        """Returns a dict: output_index -> (value, asset, is_coinbase)."""
        assert isinstance(tx_hash, str)
        assert isinstance(address, str)
        d = self.txo.get(tx_hash, {}).get(address, {})
        return {int(n): (v, asset, cb) for (n, (v, asset, cb)) in d.items()}

    @modifier
    def add_txi_addr(self, tx_hash: str, addr: str, ser: str, v: int, asset: Optional[str]) -> None:
        assert isinstance(tx_hash, str)
        assert isinstance(addr, str)
        assert isinstance(ser, str)
        assert isinstance(v, int)
        assert asset is None or isinstance(asset, str)
        if tx_hash not in self.txi:
            self.txi[tx_hash] = {}
        d = self.txi[tx_hash]
        if addr not in d:
            d[addr] = {}
        d[addr][ser] = (v, asset)

    @modifier
    def add_txo_addr(self, tx_hash: str, addr: str, n: Union[int, str], v: int, asset: Optional[str], is_coinbase: bool) -> None:
        n = str(n)
        assert isinstance(tx_hash, str)
        assert isinstance(addr, str)
        assert isinstance(n, str)
        assert isinstance(v, int)
        assert asset is None or isinstance(asset, str)
        assert isinstance(is_coinbase, bool)
        if tx_hash not in self.txo:
            self.txo[tx_hash] = {}
        d = self.txo[tx_hash]
        if addr not in d:
            d[addr] = {}
        d[addr][n] = (v, asset, is_coinbase)

    @locked
    def list_txi(self) -> Sequence[str]:
        return list(self.txi.keys())

    @locked
    def list_txo(self) -> Sequence[str]:
        return list(self.txo.keys())

    @modifier
    def remove_txi(self, tx_hash: str) -> None:
        assert isinstance(tx_hash, str)
        self.txi.pop(tx_hash, None)

    @modifier
    def remove_txo(self, tx_hash: str) -> None:
        assert isinstance(tx_hash, str)
        self.txo.pop(tx_hash, None)

    @locked
    def list_spent_outpoints(self) -> Sequence[Tuple[str, str]]:
        return [(h, n)
                for h in self.spent_outpoints.keys()
                for n in self.get_spent_outpoints(h)
        ]

    @locked
    def get_spent_outpoints(self, prevout_hash: str) -> Sequence[str]:
        assert isinstance(prevout_hash, str)
        return list(self.spent_outpoints.get(prevout_hash, {}).keys())

    @locked
    def get_spent_outpoint(self, prevout_hash: str, prevout_n: Union[int, str]) -> Optional[str]:
        assert isinstance(prevout_hash, str)
        prevout_n = str(prevout_n)
        return self.spent_outpoints.get(prevout_hash, {}).get(prevout_n)

    @modifier
    def remove_spent_outpoint(self, prevout_hash: str, prevout_n: Union[int, str]) -> None:
        assert isinstance(prevout_hash, str)
        prevout_n = str(prevout_n)
        self.spent_outpoints[prevout_hash].pop(prevout_n, None)
        if not self.spent_outpoints[prevout_hash]:
            self.spent_outpoints.pop(prevout_hash)

    @modifier
    def set_spent_outpoint(self, prevout_hash: str, prevout_n: Union[int, str], tx_hash: str) -> None:
        assert isinstance(prevout_hash, str)
        assert isinstance(tx_hash, str)
        prevout_n = str(prevout_n)
        if prevout_hash not in self.spent_outpoints:
            self.spent_outpoints[prevout_hash] = {}
        self.spent_outpoints[prevout_hash][prevout_n] = tx_hash

    @modifier
    def add_prevout_by_scripthash(self, scripthash: str, *, prevout: TxOutpoint, value: int, asset: Optional[str]) -> None:
        assert isinstance(scripthash, str)
        assert isinstance(prevout, TxOutpoint)
        assert isinstance(value, int)
        if scripthash not in self._prevouts_by_scripthash:
            self._prevouts_by_scripthash[scripthash] = set()
        self._prevouts_by_scripthash[scripthash].add((prevout.to_str(), value, asset))

    @modifier
    def remove_prevout_by_scripthash(self, scripthash: str, *, prevout: TxOutpoint, value: int) -> None:
        assert isinstance(scripthash, str)
        assert isinstance(prevout, TxOutpoint)
        assert isinstance(value, int)
        self._prevouts_by_scripthash[scripthash].discard((prevout.to_str(), value))
        if not self._prevouts_by_scripthash[scripthash]:
            self._prevouts_by_scripthash.pop(scripthash)

    @locked
    def get_prevouts_by_scripthash(self, scripthash: str) -> Set[Tuple[TxOutpoint, int, Optional[str]]]:
        assert isinstance(scripthash, str)
        prevouts_and_values = self._prevouts_by_scripthash.get(scripthash, set())
        return {(TxOutpoint.from_str(prevout), value, asset) for prevout, value, asset in prevouts_and_values}

    @modifier
    def add_transaction(self, tx_hash: str, tx: Transaction) -> None:
        assert isinstance(tx_hash, str)
        assert isinstance(tx, Transaction), tx
        # note that tx might be a PartialTransaction
        # serialize and de-serialize tx now. this might e.g. convert a complete PartialTx to a Tx
        tx = tx_from_any(str(tx))
        if not tx_hash:
            raise Exception("trying to add tx to db without txid")
        if tx_hash != tx.txid():
            raise Exception(f"trying to add tx to db with inconsistent txid: {tx_hash} != {tx.txid()}")
        # don't allow overwriting complete tx with partial tx
        tx_we_already_have = self.transactions.get(tx_hash, None)
        if tx_we_already_have is None or isinstance(tx_we_already_have, PartialTransaction):
            self.transactions[tx_hash] = tx

    @modifier
    def remove_transaction(self, tx_hash: str) -> Optional[Transaction]:
        assert isinstance(tx_hash, str)
        return self.transactions.pop(tx_hash, None)

    @locked
    def get_transaction(self, tx_hash: Optional[str]) -> Optional[Transaction]:
        if tx_hash is None:
            return None
        assert isinstance(tx_hash, str), tx_hash
        return self.transactions.get(tx_hash)

    @locked
    def list_transactions(self) -> Sequence[str]:
        return list(self.transactions.keys())

    @locked
    def get_history(self) -> Sequence[str]:
        return list(self.history.keys())

    def is_addr_in_history(self, addr: str) -> bool:
        # does not mean history is non-empty!
        assert isinstance(addr, str)
        return addr in self.history

    @locked
    def get_addr_history(self, addr: str) -> Sequence[Tuple[str, int]]:
        assert isinstance(addr, str)
        return self.history.get(addr, [])

    @modifier
    def set_addr_history(self, addr: str, hist) -> None:
        assert isinstance(addr, str)
        self.history[addr] = hist

    @modifier
    def remove_addr_history(self, addr: str) -> None:
        assert isinstance(addr, str)
        self.history.pop(addr, None)

    @locked
    def list_verified_tx(self) -> Sequence[str]:
        return list(self.verified_tx.keys())

    @locked
    def get_verified_tx(self, txid: str) -> Optional[TxMinedInfo]:
        assert isinstance(txid, str)
        if txid not in self.verified_tx:
            return None
        height, timestamp, txpos, header_hash = self.verified_tx[txid]
        return TxMinedInfo(height=height,
                           conf=None,
                           timestamp=timestamp,
                           txpos=txpos,
                           header_hash=header_hash)

    @modifier
    def add_verified_tx(self, txid: str, info: TxMinedInfo):
        assert isinstance(txid, str)
        assert isinstance(info, TxMinedInfo)
        self.verified_tx[txid] = (info.height, info.timestamp, info.txpos, info.header_hash)

    @modifier
    def remove_verified_tx(self, txid: str):
        assert isinstance(txid, str)
        self.verified_tx.pop(txid, None)

    def is_in_verified_tx(self, txid: str) -> bool:
        assert isinstance(txid, str)
        return txid in self.verified_tx

    @modifier
    def add_tx_fee_from_server(self, txid: str, fee_sat: Optional[int]) -> None:
        assert isinstance(txid, str)
        # note: when called with (fee_sat is None), rm currently saved value
        if txid not in self.tx_fees:
            self.tx_fees[txid] = TxFeesValue()
        tx_fees_value = self.tx_fees[txid]
        if tx_fees_value.is_calculated_by_us:
            return
        self.tx_fees[txid] = tx_fees_value._replace(fee=fee_sat, is_calculated_by_us=False)

    @modifier
    def add_tx_fee_we_calculated(self, txid: str, fee_sat: Optional[int]) -> None:
        assert isinstance(txid, str)
        if fee_sat is None:
            return
        assert isinstance(fee_sat, int)
        if txid not in self.tx_fees:
            self.tx_fees[txid] = TxFeesValue()
        self.tx_fees[txid] = self.tx_fees[txid]._replace(fee=fee_sat, is_calculated_by_us=True)

    @locked
    def get_tx_fee(self, txid: str, *, trust_server: bool = False) -> Optional[int]:
        assert isinstance(txid, str)
        """Returns tx_fee."""
        tx_fees_value = self.tx_fees.get(txid)
        if tx_fees_value is None:
            return None
        if not trust_server and not tx_fees_value.is_calculated_by_us:
            return None
        return tx_fees_value.fee

    @modifier
    def add_num_inputs_to_tx(self, txid: str, num_inputs: int) -> None:
        assert isinstance(txid, str)
        assert isinstance(num_inputs, int)
        if txid not in self.tx_fees:
            self.tx_fees[txid] = TxFeesValue()
        self.tx_fees[txid] = self.tx_fees[txid]._replace(num_inputs=num_inputs)

    @locked
    def get_num_all_inputs_of_tx(self, txid: str) -> Optional[int]:
        assert isinstance(txid, str)
        tx_fees_value = self.tx_fees.get(txid)
        if tx_fees_value is None:
            return None
        return tx_fees_value.num_inputs

    @locked
    def get_num_ismine_inputs_of_tx(self, txid: str) -> int:
        assert isinstance(txid, str)
        txins = self.txi.get(txid, {})
        return sum([len(tupls) for addr, tupls in txins.items()])

    @modifier
    def remove_tx_fee(self, txid: str) -> None:
        assert isinstance(txid, str)
        self.tx_fees.pop(txid, None)

    @locked
    def num_change_addresses(self) -> int:
        return len(self.change_addresses)

    @locked
    def num_receiving_addresses(self) -> int:
        return len(self.receiving_addresses)

    @locked
    def get_change_addresses(self, *, slice_start=None, slice_stop=None) -> List[str]:
        # note: slicing makes a shallow copy
        return self.change_addresses[slice_start:slice_stop]

    @locked
    def get_receiving_addresses(self, *, slice_start=None, slice_stop=None) -> List[str]:
        # note: slicing makes a shallow copy
        return self.receiving_addresses[slice_start:slice_stop]

    @modifier
    def add_change_address(self, addr: str) -> None:
        assert isinstance(addr, str)
        self._addr_to_addr_index[addr] = (1, len(self.change_addresses))
        self.change_addresses.append(addr)

    @modifier
    def add_receiving_address(self, addr: str) -> None:
        assert isinstance(addr, str)
        self._addr_to_addr_index[addr] = (0, len(self.receiving_addresses))
        self.receiving_addresses.append(addr)

    @locked
    def get_address_index(self, address: str) -> Optional[Sequence[int]]:
        assert isinstance(address, str)
        return self._addr_to_addr_index.get(address)

    @modifier
    def add_imported_address(self, addr: str, d: dict) -> None:
        assert isinstance(addr, str)
        self.imported_addresses[addr] = d

    @modifier
    def remove_imported_address(self, addr: str) -> None:
        assert isinstance(addr, str)
        self.imported_addresses.pop(addr)

    @locked
    def has_imported_address(self, addr: str) -> bool:
        assert isinstance(addr, str)
        return addr in self.imported_addresses

    @locked
    def get_imported_addresses(self) -> Sequence[str]:
        return list(sorted(self.imported_addresses.keys()))

    @locked
    def get_imported_address(self, addr: str) -> Optional[dict]:
        assert isinstance(addr, str)
        return self.imported_addresses.get(addr)

    def load_addresses(self, wallet_type):
        """ called from Abstract_Wallet.__init__ """
        if wallet_type == 'imported':
            self.imported_addresses = self.get_dict('addresses')  # type: Dict[str, dict]
        else:
            self.get_dict('addresses')
            for name in ['receiving', 'change']:
                if name not in self.data['addresses']:
                    self.data['addresses'][name] = []
            self.change_addresses = self.data['addresses']['change']
            self.receiving_addresses = self.data['addresses']['receiving']
            self._addr_to_addr_index = {}  # type: Dict[str, Sequence[int]]  # key: address, value: (is_change, index)
            for i, addr in enumerate(self.receiving_addresses):
                self._addr_to_addr_index[addr] = (0, i)
            for i, addr in enumerate(self.change_addresses):
                self._addr_to_addr_index[addr] = (1, i)

    def load_assets(self):
        """ called from Abstract_Wallet.__init__ """
        if 'assets_to_watch' not in self.data:
            self.data['assets_to_watch'] = set()
        self.assets_to_watch = self.get('assets_to_watch')  # type: Set[str]
        self.verified_asset_metadata = self.get_dict('verified_asset_metadata')  # type: Dict[str, Tuple[AssetMetadata, Tuple[TxOutpoint, int], Tuple[TxOutpoint, int] | None, Tuple[TxOutpoint, int] | None]]       
        self.non_deterministic_vouts = self.get_dict('non_deterministic_txo_scriptpubkey')

    @locked
    def get_non_deterministic_txo_lockingscript(self, outpoint: TxOutpoint) -> Optional[bytes]:
        assert isinstance(outpoint, TxOutpoint)
        return self.non_deterministic_vouts.get(outpoint.to_str(), None)

    @modifier
    def add_non_deterministic_txo_lockingscript(self, outpoint: TxOutpoint, script: bytes):
        assert isinstance(outpoint, TxOutpoint)
        assert isinstance(script, bytes)
        self.non_deterministic_vouts[outpoint.to_str()] = script

    @locked
    def get_assets_to_watch(self) -> Sequence[str]:
        return list(sorted(self.assets_to_watch))
    
    @locked
    def is_watching_asset(self, asset: str) -> bool:
        assert isinstance(asset, str)
        return asset in self.assets_to_watch

    @modifier
    def add_asset_to_watch(self, asset: str):
        assert isinstance(asset, str)
        assert (error := get_error_for_asset_name(asset) is None), error
        self.assets_to_watch.add(asset)

    @locked
    def get_asset_metadata(self, asset) -> Optional[AssetMetadata]:
        assert isinstance(asset, str)
        result = self.verified_asset_metadata.get(asset)
        if result is None: return None
        return result[0]

    @modifier
    def add_verified_asset_metadata(self, asset: str, metadata: AssetMetadata, source_tup: Tuple[TxOutpoint, int], source_divisions_tup: Tuple[TxOutpoint, int] | None, source_associated_data_tup: Tuple[TxOutpoint, int] | None):
        assert isinstance(asset, str)
        assert isinstance(metadata, AssetMetadata)
        assert isinstance(source_tup, Tuple)
        assert len(source_tup) == 2
        assert isinstance(source_tup[0], TxOutpoint)
        assert isinstance(source_tup[1], int)
        if source_divisions_tup is not None:
            assert isinstance(source_divisions_tup, Tuple)
            assert len(source_divisions_tup) == 2
            assert isinstance(source_divisions_tup[0], TxOutpoint)
            assert isinstance(source_divisions_tup[1], int)
        if source_associated_data_tup is not None:
            assert isinstance(source_associated_data_tup, Tuple)
            assert len(source_associated_data_tup) == 2
            assert isinstance(source_associated_data_tup[0], TxOutpoint)
            assert isinstance(source_associated_data_tup[1], int)

        self.verified_asset_metadata[asset] = metadata, source_tup, source_divisions_tup, source_associated_data_tup

    @locked
    def get_verified_asset_metadata(self, asset: str) -> Optional[AssetMetadata]:
        assert isinstance(asset, str)
        result = self.verified_asset_metadata.get(asset, None)
        if not result: return None
        return result[0]
    
    @locked
    def get_verified_asset_metadata_base_source(self, asset: str) -> Optional[Tuple[TxOutpoint, int]]:
        assert isinstance(asset, str)
        result = self.verified_asset_metadata.get(asset, None)
        if not result: return None
        return result[1][0], result[1][1]
    
    @locked
    def get_assets_verified_after_height(self, height: int) -> Sequence[str]:
        assert isinstance(height, int)
        assets = []
        for asset, (_, (_, verified_height), _, _) in self.verified_asset_metadata.items():
            if verified_height > height:
                assets.append(asset)
        return assets

    @modifier
    def remove_verified_asset_metadata(self, asset: str):
        assert isinstance(asset, str)
        return self.verified_asset_metadata.pop(asset, None)

    @profiler
    def _load_transactions(self):
        self.data = StoredDict(self.data, self, [])
        # references in self.data
        # TODO make all these private
        # txid -> address -> prev_outpoint -> value
        self.txi = self.get_dict('txi')                          # type: Dict[str, Dict[str, Dict[str, Tuple[int, Optional[str]]]]]
        # txid -> address -> output_index -> (value, is_coinbase)
        self.txo = self.get_dict('txo')                          # type: Dict[str, Dict[str, Dict[str, Tuple[int, Optional[str], bool]]]]
        self.transactions = self.get_dict('transactions')        # type: Dict[str, Transaction]
        self.spent_outpoints = self.get_dict('spent_outpoints')  # txid -> output_index -> next_txid
        self.history = self.get_dict('addr_history')             # address -> list of (txid, height)
        self.verified_tx = self.get_dict('verified_tx3')         # txid -> (height, timestamp, txpos, header_hash)
        self.tx_fees = self.get_dict('tx_fees')                  # type: Dict[str, TxFeesValue]
        # scripthash -> set of (outpoint, value)
        self._prevouts_by_scripthash = self.get_dict('prevouts_by_scripthash')  # type: Dict[str, Set[Tuple[str, int]]]
        # remove unreferenced tx
        for tx_hash in list(self.transactions.keys()):
            if not self.get_txi_addresses(tx_hash) and not self.get_txo_addresses(tx_hash):
                self.logger.info(f"removing unreferenced tx: {tx_hash}")
                self.transactions.pop(tx_hash)
        # remove unreferenced outpoints
        for prevout_hash in self.spent_outpoints.keys():
            d = self.spent_outpoints[prevout_hash]
            for prevout_n, spending_txid in list(d.items()):
                if spending_txid not in self.transactions:
                    self.logger.info("removing unreferenced spent outpoint")
                    d.pop(prevout_n)

    @modifier
    def clear_history(self):
        self.txi.clear()
        self.txo.clear()
        self.spent_outpoints.clear()
        self.transactions.clear()
        self.history.clear()
        self.verified_tx.clear()
        self.tx_fees.clear()
        self._prevouts_by_scripthash.clear()

    def _convert_dict(self, path, key, v):
        if key == 'transactions':
            # note: for performance, "deserialize=False" so that we will deserialize these on-demand
            v = dict((k, tx_from_any(x, deserialize=False)) for k, x in v.items())
        if key == 'invoices':
            v = dict((k, Invoice(**x)) for k, x in v.items())
        if key == 'payment_requests':
            v = dict((k, Request(**x)) for k, x in v.items())
        elif key == 'adds':
            v = dict((k, UpdateAddHtlc.from_tuple(*x)) for k, x in v.items())
        elif key == 'fee_updates':
            v = dict((k, FeeUpdate(**x)) for k, x in v.items())
        elif key == 'submarine_swaps':
            v = dict((k, SwapData(**x)) for k, x in v.items())
        elif key == 'imported_channel_backups':
            v = dict((k, ImportedChannelBackupStorage(**x)) for k, x in v.items())
        elif key == 'onchain_channel_backups':
            v = dict((k, OnchainChannelBackupStorage(**x)) for k, x in v.items())
        elif key == 'tx_fees':
            v = dict((k, TxFeesValue(*x)) for k, x in v.items())
        elif key == 'prevouts_by_scripthash':
            v = dict((k, {(prevout, value, asset) for (prevout, value, asset) in x}) for k, x in v.items())
        elif key == 'buckets':
            v = dict((k, ShachainElement(bfh(x[0]), int(x[1]))) for k, x in v.items())
        elif key == 'data_loss_protect_remote_pcp':
            v = dict((k, bfh(x)) for k, x in v.items())
        elif key == 'verified_asset_metadata':
            v = dict((k, (
                AssetMetadata(**metadata),
                (TxOutpoint.from_json(tup1[0]), tup1[1]),
                (TxOutpoint.from_json(tup2[0]), tup2[1]) if tup2 else None,
                (TxOutpoint.from_json(tup3[0]), tup3[1]) if tup3 else None,
            )) for k, (metadata, tup1, tup2, tup3) in v.items())
        elif key == 'non_deterministic_txo_scriptpubkey':
            v = dict((k, bytes.fromhex(x)) for k, x in v.items())
        # convert htlc_id keys to int
        if key in ['adds', 'locked_in', 'settles', 'fails', 'fee_updates', 'buckets',
                   'unacked_updates', 'unfulfilled_htlcs', 'fail_htlc_reasons', 'onion_keys']:
            v = dict((int(k), x) for k, x in v.items())
        # convert keys to HTLCOwner
        if key == 'log' or (path and path[-1] in ['locked_in', 'fails', 'settles']):
            if "1" in v:
                v[LOCAL] = v.pop("1")
                v[REMOTE] = v.pop("-1")
        return v

    def _convert_value(self, path, key, v):
        if key == 'local_config':
            v = LocalConfig(**v)
        elif key == 'remote_config':
            v = RemoteConfig(**v)
        elif key == 'constraints':
            v = ChannelConstraints(**v)
        elif key == 'funding_outpoint':
            v = Outpoint(**v)
        elif key == 'channel_type':
            v = ChannelType(v)
        elif key == 'db_metadata':
            v = DBMetadata(**v)
        elif key == 'assets_to_watch':
            v = set(v)
        return v

    def _should_convert_to_stored_dict(self, key) -> bool:
        if key == 'keystore':
            return False
        multisig_keystore_names = [('x%d/' % i) for i in range(1, 16)]
        if key in multisig_keystore_names:
            return False
        return True

    def write(self, storage: 'WalletStorage'):
        with self.lock:
            self._write(storage)

    @profiler
    def _write(self, storage: 'WalletStorage'):
        if threading.current_thread().daemon:
            self.logger.warning('daemon thread cannot write db')
            return
        if not self.modified():
            return
        json_str = self.dump(human_readable=not storage.is_encrypted())
        storage.write(json_str)
        self.set_modified(False)

    def is_ready_to_be_used_by_wallet(self):
        return not self.requires_upgrade() and self._called_after_upgrade_tasks

    def split_accounts(self, root_path):
        from .storage import WalletStorage
        out = []
        result = self.get_split_accounts()
        for data in result:
            path = root_path + '.' + data['suffix']
            storage = WalletStorage(path)
            db = WalletDB(json.dumps(data), manual_upgrades=False)
            db._called_after_upgrade_tasks = False
            db.upgrade()
            db.write(storage)
            out.append(path)
        return out

    def get_action(self):
        action = run_hook('get_action', self)
        return action

    def load_plugins(self):
        wallet_type = self.get('wallet_type')
        if wallet_type in plugin_loaders:
            plugin_loaders[wallet_type]()

    def set_keystore_encryption(self, enable):
        self.put('use_encryption', enable)
