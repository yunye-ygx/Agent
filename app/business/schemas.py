"""Public request/response contracts for the after-sales business domain."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SUPPORTED_MODELS = ("NX100", "NX200")
MAX_ORDER_TOTAL = Decimal("999999999999.99")
MAX_ORDER_ITEMS = 50
MAX_PAGE_SIZE = 100

# Order input


def parse_money(value: object, *, max_digits: int) -> Decimal:
    if isinstance(value, (bool, float)):
        raise ValueError("金额必须使用十进制字符串，避免浮点误差")  # noqa: TRY004
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        # Pydantic only converts ValueError into a validation error.
        raise ValueError("金额格式不正确") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError("金额必须为非负数")
    if amount.as_tuple().exponent < -2:
        raise ValueError("金额最多两位小数")
    digits = len(amount.as_tuple().digits)
    if digits > max_digits:
        raise ValueError("金额超出允许范围")
    return amount.quantize(Decimal("0.01"))


class OrderItemCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_name: str = Field(min_length=1, max_length=200)
    product_model: Literal["NX100", "NX200"]
    quantity: int = Field(ge=1, le=1000, strict=True)
    unit_price: Decimal

    @field_validator("product_name")
    @classmethod
    def _name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("产品名称不能为空")
        return value

    @field_validator("unit_price", mode="before")
    @classmethod
    def _price(cls, value: object) -> Decimal:
        return parse_money(value, max_digits=12)


class OrderCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_no: str = Field(min_length=1, max_length=64)
    user_id: str = Field(min_length=1, max_length=36)
    items: list[OrderItemCreate] = Field(min_length=1, max_length=MAX_ORDER_ITEMS)

    @field_validator("order_no")
    @classmethod
    def _order_no(cls, value: str) -> str:
        value = value.strip().upper()
        if not value:
            raise ValueError("订单号不能为空")
        return value


class OrderStatusUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["recorded", "completed", "cancelled"]


class DeviceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    serial_no: str = Field(min_length=1, max_length=64)
    order_item_id: str = Field(min_length=1, max_length=36)
    model_code: Literal["NX100", "NX200"]
    firmware_version: str | None = Field(default=None, max_length=32)
    purchased_on: date
    warranty_until: date

    @field_validator("serial_no")
    @classmethod
    def _serial(cls, value: str) -> str:
        value = value.strip().upper()
        allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
        if not value or not set(value) <= allowed:
            raise ValueError("设备编号只能包含大写字母、数字、下划线和连字符")
        return value

    @field_validator("firmware_version")
    @classmethod
    def _firmware(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            return None
        if not set(value) <= set("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz._-"):
            raise ValueError("固件版本格式不正确")
        return value

    @model_validator(mode="after")
    def _dates(self):
        if self.warranty_until < self.purchased_on:
            raise ValueError("保修截止日不能早于购买日期")
        if not date(2000, 1, 1) <= self.purchased_on <= date(2100, 12, 31):
            raise ValueError("购买日期超出支持范围")
        if self.warranty_until > date(2100, 12, 31):
            raise ValueError("保修截止日超出支持范围")
        return self


class OrderItemResponse(BaseModel):
    id: str
    product_name: str
    product_model: str
    quantity: int
    unit_price: str
    line_amount: str


class OrderSummary(BaseModel):
    id: str
    order_no: str
    status: str
    total_amount: str
    currency: str
    item_count: int
    created_at: str
    customer_username: str | None = None
    customer_display_name: str | None = None


class OrderDetail(OrderSummary):
    user_id: str
    items: list[OrderItemResponse]


class OrderPage(BaseModel):
    items: list[OrderSummary]
    total: int
    page: int
    page_size: int


class DeviceSummary(BaseModel):
    id: str
    serial_no: str
    model_code: str
    firmware_version: str | None
    purchased_on: str
    warranty_until: str
    warranty_status: Literal["in_warranty", "out_of_warranty"]
    warranty_active: bool
    status: str
    order_id: str
    order_no: str
    order_item_id: str
    product_name: str
    customer_username: str | None = None
    customer_display_name: str | None = None


class DevicePage(BaseModel):
    items: list[DeviceSummary]
    total: int
    page: int
    page_size: int


# Tickets and idempotent operations

TICKET_STATUS_VALUES = ("open", "in_progress", "resolved", "closed")


class TicketCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    serial_no: str = Field(min_length=1, max_length=64)
    subject: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=4000)

    @field_validator("serial_no")
    @classmethod
    def _serial(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("subject", "description")
    @classmethod
    def _text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("内容不能为空")
        return value


class TicketCreateRequest(TicketCreate):
    operation_id: str = Field(min_length=1, max_length=36)


class TicketEventCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1, strict=True)
    message: str = Field(min_length=1, max_length=2000)
    to_status: Literal["open", "in_progress", "resolved", "closed"] | None = None

    @field_validator("message")
    @classmethod
    def _message(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("处理说明不能为空")
        return value


class TicketEventResponse(BaseModel):
    id: str
    event_type: str
    from_status: str | None
    to_status: str | None
    message: str
    actor_username: str
    actor_display_name: str
    created_at: str


class TicketSummary(BaseModel):
    id: str
    ticket_no: str
    subject: str
    status: str
    version: int
    created_at: str
    updated_at: str
    device_id: str
    device_serial_no: str
    device_model: str
    customer_username: str
    customer_display_name: str


class TicketDetail(TicketSummary):
    description: str
    events: list[TicketEventResponse]
    events_total: int
    events_page: int
    events_page_size: int


class TicketPage(BaseModel):
    items: list[TicketSummary]
    total: int
    page: int
    page_size: int


class OperationReserve(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["create_ticket"]
    parameters: dict


class OperationResponse(BaseModel):
    id: str
    action: str
    status: str
    expires_at: str
    expired: bool = False
    result_resource_id: str | None = None
    result: dict | None = None
    resource_available: bool = False
    created_at: str
    completed_at: str | None = None


# 售后资料开放开关（documents.support_open）


class DocumentSupportUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    open: bool


class DocumentSupportResponse(BaseModel):
    document_id: str
    filename: str
    knowledge_base_id: str
    document_status: str
    support_open: bool
    searchable: bool
    version_no: int | None = None
    published_at: str | None = None
