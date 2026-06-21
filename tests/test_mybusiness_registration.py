from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.services.mybusiness import MyBusinessService, build_course_enrollment_payload, pointer


class FakeMyBusinessService(MyBusinessService):
    def __init__(self, objects: dict[tuple[str, str], dict], class_rows: dict[str, list[dict]] | None = None):
        settings = SimpleNamespace(
            mybusiness_app_id="app",
            mybusiness_master_key="key",
            mybusiness_base_url="https://example.test/parse",
            mybusiness_timeout_seconds=1,
        )
        super().__init__(settings)
        self.objects = objects
        self.class_rows = class_rows or {}
        self.posts: list[tuple[str, dict]] = []

    async def _get_object(self, table_name: str, object_id: str, params: dict | None = None) -> dict | None:
        return self.objects.get((table_name, object_id))

    async def _get_class(self, table_name: str, params: dict) -> list[dict]:
        return self.class_rows.get(table_name, [])

    async def _post_class(self, table_name: str, payload: dict) -> dict:
        self.posts.append((table_name, payload))
        return {"objectId": "enrollment1", **payload}


def future_iso(days: int = 5) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def open_course(course_id: str = "course1") -> dict:
    return {
        "objectId": course_id,
        "Name": "Forklift course",
        "StartDate": {"__type": "Date", "iso": future_iso()},
        "StatusId": {"objectId": "U3IMyC5c9H", "Name": "Open", "IsOpen": True},
        "ProductCategory": {"objectId": "cat1", "Name": "Forklift", "Code": "80001"},
        "MaxCapacity": 10,
        "RegisteredStudents": 7,
    }


def test_check_customer_registration_eligibility_allows_valid_account_course_and_sale() -> None:
    service = FakeMyBusinessService(
        {
            ("Accounts", "account1"): {"objectId": "account1", "Name": "Test Customer", "Delete": False},
            ("Courses", "course1"): open_course(),
            ("Sales", "sale1"): {
                "objectId": "sale1",
                "AccountId": {"objectId": "account1"},
                "SaleStatusId": {"objectId": "status1", "Name": "New"},
            },
        }
    )

    result = run_async(
        service.check_customer_registration_eligibility(account_id="account1", course_id="course1", sale_id="sale1")
    )

    assert result["can_register"] is True
    assert result["blocking_reasons"] == []
    assert result["course"]["available_seats"] == 3
    assert result["sale"]["belongs_to_account"] is True


def test_check_customer_registration_eligibility_blocks_existing_future_active_enrollment() -> None:
    service = FakeMyBusinessService(
        {
            ("Accounts", "account1"): {"objectId": "account1", "Name": "Test Customer", "Delete": False},
            ("Courses", "course1"): open_course(),
        },
        {
            "CourseEnrollment": [
                {
                    "objectId": "enrollment-existing",
                    "CourseEnrollmentStatusId": {"objectId": "0BbaSYbE8x", "Name": "Registered"},
                    "CourseId": open_course("course-other"),
                    "PayingStatus": {"objectId": "0eBXa9VeT8", "Name": "Paid"},
                }
            ]
        },
    )

    result = run_async(service.check_customer_registration_eligibility(account_id="account1", course_id="course1"))

    assert result["can_register"] is False
    assert result["blocking_reasons"] == ["CUSTOMER_ALREADY_HAS_FUTURE_ACTIVE_ENROLLMENT"]
    assert result["existing_future_active_enrollments"][0]["enrollment_id"] == "enrollment-existing"


def test_register_customer_to_course_dry_run_builds_payload_without_posting() -> None:
    service = FakeMyBusinessService(
        {
            ("Accounts", "account1"): {"objectId": "account1", "Name": "Test Customer", "Delete": False},
            ("Courses", "course1"): open_course(),
            ("Sales", "sale1"): {"objectId": "sale1", "AccountId": {"objectId": "account1"}},
        }
    )

    result = run_async(
        service.register_customer_to_course(
            account_id="account1",
            course_id="course1",
            sale_id="sale1",
            payment_status="PAID",
            amount_paid=100,
            dry_run=True,
        )
    )

    assert result["created"] is False
    assert result["dry_run"] is True
    assert service.posts == []
    assert result["would_create_payload"]["PayingStatus"] == pointer("PayingStatusList", "0eBXa9VeT8")
    assert result["would_create_payload"]["AmountPaid"] == 100


def test_register_customer_to_course_does_not_post_when_sale_belongs_to_other_account() -> None:
    service = FakeMyBusinessService(
        {
            ("Accounts", "account1"): {"objectId": "account1", "Name": "Test Customer", "Delete": False},
            ("Courses", "course1"): open_course(),
            ("Sales", "sale1"): {"objectId": "sale1", "AccountId": {"objectId": "other-account"}},
        }
    )

    result = run_async(
        service.register_customer_to_course(
            account_id="account1",
            course_id="course1",
            sale_id="sale1",
            payment_status="UNPAID",
            dry_run=False,
            payment_verified=True,
        )
    )

    assert result["created"] is False
    assert result["eligibility"]["blocking_reasons"] == ["SALE_DOES_NOT_BELONG_TO_ACCOUNT"]
    assert service.posts == []


def test_register_customer_to_course_blocks_real_write_without_system_payment_verification() -> None:
    service = FakeMyBusinessService(
        {
            ("Accounts", "account1"): {"objectId": "account1", "Name": "Test Customer", "Delete": False},
            ("Courses", "course1"): open_course(),
            ("Sales", "sale1"): {"objectId": "sale1", "AccountId": {"objectId": "account1"}},
        }
    )

    result = run_async(
        service.register_customer_to_course(
            account_id="account1",
            course_id="course1",
            sale_id="sale1",
            payment_status="PAID",
            dry_run=False,
        )
    )

    assert result["created"] is False
    assert result["eligibility"]["blocking_reasons"] == ["PAYMENT_NOT_VERIFIED_BY_SYSTEM"]
    assert service.posts == []


def test_build_course_enrollment_payload_uses_required_pointers() -> None:
    payload = build_course_enrollment_payload(
        account_id="account1",
        course=open_course(),
        sale_id="sale1",
        payment_status="COMPANY_INVOICE",
        amount_paid=0,
        comment=None,
    )

    assert payload["AccountId"] == pointer("Accounts", "account1")
    assert payload["AccountMainId"] == pointer("Accounts", "account1")
    assert payload["CourseId"] == pointer("Courses", "course1")
    assert payload["SaleId"] == pointer("Sales", "sale1")
    assert payload["CourseEnrollmentStatusId"] == pointer("CourseEnrollmentStatus", "0BbaSYbE8x")
    assert payload["PayingStatus"] == pointer("PayingStatusList", "Hhz193kwFu")


def run_async(coro):
    import asyncio

    return asyncio.run(coro)
