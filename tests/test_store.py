"""Manually logged fiat transfers: the only record Binance TH's API can't
give us, so it has to survive on its own table, in its own words."""
from app.models import D, Transfer
from app.store import Store


def test_manual_transfer_round_trips(tmp_path):
    store = Store(tmp_path / "s.db")
    row_id = store.add_manual_transfer("DEPOSIT", "THB", D("5000"), 1_700_000_000_000,
                                       "salary")
    rows = store.manual_transfers()

    assert len(rows) == 1
    t = rows[0]
    assert t.transfer_id == str(row_id)
    assert t.kind == "DEPOSIT"
    assert t.asset == "THB"
    assert t.amount == D("5000")
    assert t.time == 1_700_000_000_000
    assert t.note == "salary"


def test_manual_transfer_can_be_deleted(tmp_path):
    store = Store(tmp_path / "s.db")
    row_id = store.add_manual_transfer("WITHDRAWAL", "THB", D("1000"), 1_700_000_000_000)

    assert store.delete_manual_transfer(row_id) is True
    assert store.manual_transfers() == []
    assert store.delete_manual_transfer(row_id) is False, "already gone"


def test_all_transfers_merges_reported_and_manual_by_time(tmp_path):
    store = Store(tmp_path / "s.db")
    t0 = 1_700_000_000_000
    day = 86_400_000
    store.upsert_transfers([
        Transfer(transfer_id="r1", kind="DEPOSIT", asset="USDT",
                 amount=D("10"), fee=D("0"), time=t0 + day),
    ])
    store.add_manual_transfer("DEPOSIT", "THB", D("5000"), t0)

    merged = store.all_transfers()

    assert [t.transfer_id for t in merged] == ["1", "r1"], \
        "manual entry (t0) must sort before the reported one (t0 + a day)"


def test_counts_includes_manual_transfers(tmp_path):
    store = Store(tmp_path / "s.db")
    store.add_manual_transfer("DEPOSIT", "THB", D("1"), 1_700_000_000_000)

    assert store.counts()["manual_transfers"] == 1
