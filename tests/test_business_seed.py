import shutil
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.business import seed_data
from app.business.models import BusinessUser, Device, Order, OrderItem, Ticket, TicketEvent
from app.business.seed_data import SeedError, apply_dataset, load_dataset
from app.cli import seed_business
from app.cli.import_support_knowledge import load_manifest

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "samples" / "after-sales-business-v1"
PASSWORDS = {
    "admin": "admin-password-2026",
    "alice": "alice-password-2026",
    "bob": "bob-password-2026",
    "carol": "carol-password-2026",
}


def test_dataset_counts_references_and_warranty_are_consistent():
    dataset = load_dataset(DATA, as_of=dataset_date())
    assert dataset.counts == {
        "users": 4,
        "admins": 1,
        "customers": 3,
        "orders": 12,
        "order_items": 12,
        "devices": 18,
        "tickets": 12,
        "ticket_events": 35,
    }
    devices = {device["id"]: device for device in dataset.devices}
    items = {item["id"]: item for order in dataset.orders for item in order["items"]}
    order_by_item = {
        item["id"]: order for order in dataset.orders for item in order["items"]
    }
    for device in dataset.devices:
        item = items[device["order_item_id"]]
        assert device["user_id"] == order_by_item[item["id"]]["user_id"]
        assert device["model_code"] == item["product_model"]
        assert device["_warranty_until"] >= device["_purchased_on"]
    for ticket in dataset.tickets:
        assert ticket["user_id"] == devices[ticket["device_id"]]["user_id"]
        assert ticket["events"][-1]["to_status"] == ticket["status"]
    assert sum(1 for device in dataset.devices if device["status"] == "retired") == 1
    assert {ticket["status"] for ticket in dataset.tickets} == {
        "open",
        "in_progress",
        "resolved",
        "closed",
    }
    in_warranty = sum(
        1
        for device in dataset.devices
        if dataset.as_of <= device["_warranty_until"]
    )
    assert 0 < in_warranty < len(dataset.devices)


def dataset_date():
    from datetime import date

    return date(2026, 10, 26)


def test_dry_run_never_connects(monkeypatch, capsys):
    class ExplodingFactory:
        def __call__(self):
            raise AssertionError("dry-run must not open a database session")

    monkeypatch.setattr(seed_business, "AsyncSessionFactory", ExplodingFactory())
    assert seed_business.main(["--dry-run", "--as-of", "2026-10-26"]) == 0
    output = capsys.readouterr().out
    assert "dry-run" in output
    assert "用户 4" in output


def test_wrong_as_of_is_rejected():
    with pytest.raises(SeedError, match="manifest"):
        load_dataset(DATA, as_of=dataset_date().replace(day=25))


def test_manifest_hash_mismatch_is_rejected(tmp_path):
    copied = tmp_path / "data"
    shutil.copytree(DATA, copied)
    users = (copied / "users.json").read_text(encoding="utf-8")
    (copied / "users.json").write_text(users.replace("爱丽丝", "被篡改"), encoding="utf-8")
    with pytest.raises(SeedError, match="哈希"):
        load_dataset(copied, as_of=dataset_date())


@pytest.mark.asyncio
async def test_apply_is_idempotent_and_preserves_edits(business_db):
    dataset = load_dataset(DATA, as_of=dataset_date())
    report = await apply_dataset(business_db, dataset, PASSWORDS)
    assert report["users_created"] == 4
    assert report["orders_created"] == 12
    assert report["devices_created"] == 18
    assert report["tickets_created"] == 12
    for model, expected in (
        (BusinessUser, 4),
        (Order, 12),
        (OrderItem, 12),
        (Device, 18),
        (Ticket, 12),
        (TicketEvent, 35),
    ):
        assert (
            await business_db.execute(select(func.count()).select_from(model))
        ).scalar_one() == expected

    alice = (
        await business_db.execute(
            select(BusinessUser).where(BusinessUser.username == "alice")
        )
    ).scalar_one()
    alice.display_name = "爱丽丝（已修改）"
    await business_db.commit()

    again = await apply_dataset(business_db, dataset, PASSWORDS)
    assert again["users_created"] == 0
    assert again["orders_skipped"] == 12
    assert again["devices_skipped"] == 18
    assert again["tickets_skipped"] == 12
    stored = (
        await business_db.execute(
            select(BusinessUser).where(BusinessUser.username == "alice")
        )
    ).scalar_one()
    assert stored.display_name == "爱丽丝（已修改）"
    assert (
        await business_db.execute(select(func.count()).select_from(TicketEvent))
    ).scalar_one() == 35


@pytest.mark.asyncio
async def test_conflicting_existing_username_stops_without_writing(business_db):
    dataset = load_dataset(DATA, as_of=dataset_date())
    business_db.add(
        BusinessUser(
            id="someone-else",
            username="alice",
            display_name="另一个账号",
            password_hash="unused",
            role="customer",
        )
    )
    await business_db.commit()
    with pytest.raises(SeedError, match="ID 不同"):
        await apply_dataset(business_db, dataset, PASSWORDS)
    assert (
        await business_db.execute(select(func.count()).select_from(Order))
    ).scalar_one() == 0
    assert (
        await business_db.execute(select(func.count()).select_from(BusinessUser))
    ).scalar_one() == 1


@pytest.mark.asyncio
async def test_missing_password_rolls_back_every_new_user(business_db):
    dataset = load_dataset(DATA, as_of=dataset_date())
    with pytest.raises(SeedError, match="密码"):
        await apply_dataset(business_db, dataset, {"admin": PASSWORDS["admin"]})
    assert (
        await business_db.execute(select(func.count()).select_from(BusinessUser))
    ).scalar_one() == 0


def test_knowledge_manifest_only_opens_selected_corpus():
    manifest = load_manifest()
    sources = {document["source"] for document in manifest["documents"]}
    assert sources == {
        "01_nx100_manual.md",
        "02_nx200_manual.md",
        "03_nx100_faults.md",
        "04_nx200_network.md",
        "05_safety_and_service.md",
        "06_nx100_firmware_notice.md",
    }
    assert "07_archived_calibration.md" in manifest["excluded_sources"]
    assert "eval/dev_cases.jsonl" in manifest["excluded_sources"]
    assert "model_mappings" not in manifest and "model_codes" not in manifest
    assert all(document["title"] for document in manifest["documents"])
    assert not any("ticket" in source for source in sources)


def test_seed_data_module_exposes_describe():
    dataset = load_dataset(DATA, as_of=dataset_date())
    assert "工单 12" in seed_data.describe(dataset)
