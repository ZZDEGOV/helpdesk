"""Pydantic models: the HTML form bodies and the JSON API responses.

Form models are used as `Annotated[Model, Form()]`, so a route receives one
validated object instead of a dozen loose parameters, and /docs shows each
form's real fields and allowed values.

The Literal types are built from config, so adding a status or channel there
is still a config-only change.
"""
from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from . import config


def _blank_to_none(value):
    return None if isinstance(value, str) and not value.strip() else value


# Browsers submit an empty string for an untouched date or select. Treat it as
# "not given" rather than a validation error.
Blankable = BeforeValidator(_blank_to_none)

Status = Literal[tuple(config.STATUSES)]            # type: ignore[valid-type]
Channel = Literal[tuple(config.CHANNELS)]           # type: ignore[valid-type]
MessageKind = Literal[tuple(config.MESSAGE_KINDS)]  # type: ignore[valid-type]
Role = Literal[tuple(config.ROLES)]                 # type: ignore[valid-type]
Priority = Annotated[int, Field(ge=1, le=5, description="1 = critical, 5 = trivial")]
LocalDateTime = Annotated[datetime | None, Blankable,
                          Field(description="Local time, as from <input type=datetime-local>")]
LocalDate = Annotated[date | None, Blankable,
                      Field(description="Local date, as from <input type=date>")]
OptionalId = Annotated[int | None, Blankable]


# ---------------------------------------------------------------- forms: tickets

class TicketCreate(BaseModel):
    subject: str = Field(min_length=1, max_length=300)
    body: str = ""
    customer_name: str = ""
    customer_email: str = Field("", description="Matched against existing customers")
    customer_phone: str = ""
    account_id: str = ""
    city: str = ""
    channel: Channel = "unknown"
    category: str = ""
    subcategory: str = ""
    priority: Priority = 3
    ada: bool = Field(False, description="Accessibility (ADA) related")
    logged_by: str = Field("", description="CSR username, if forwarded")
    received_at: LocalDateTime = Field(None, description="Defaults to now")
    due_at: LocalDate = Field(None, description="Target date; the deadline is end of work day")
    tags: str = Field("", description="Comma separated")


class TicketUpdate(BaseModel):
    subject: str = Field(min_length=1, max_length=300)
    priority: Priority = 3
    category: str = ""
    subcategory: str = ""
    channel: Channel = "unknown"
    due_at: LocalDate = None
    resolution: str = ""
    root_cause: str = ""


class StatusChange(BaseModel):
    status: Status


class MessageCreate(BaseModel):
    kind: MessageKind = "note"
    body: str = Field(min_length=1)
    author: str = ""
    occurred_at: LocalDateTime = Field(None, description="Defaults to now")
    track_questions: bool = Field(False, description="Pull questions out of the body")


class FollowUp(BaseModel):
    follow_up_at: LocalDateTime = None
    follow_up_note: str = ""
    done: bool = Field(False, description="Mark the current follow-up done instead")


# ---------------------------------------------------------------- forms: customers

class CustomerEdit(BaseModel):
    name: str = ""
    account_id: str = ""
    address: str = ""
    city: str = ""
    zip: str = ""


class ContactChange(BaseModel):
    action: Literal["add_email", "primary_email", "remove_email",
                    "add_phone", "primary_phone", "remove_phone"]
    value: str = ""
    label: str = ""


# ---------------------------------------------------------------- forms: replies

class ReplyTemplateSave(BaseModel):
    template_id: OptionalId = Field(None, description="Omit to create a new reply")
    name: str = Field(min_length=1)
    category: str = ""
    body: str = Field(min_length=1)


# ---------------------------------------------------------------- forms: accounts

class Login(BaseModel):
    username: str
    password: str = ""


class Setup(BaseModel):
    username: str = Field(min_length=1)
    display_name: str = ""
    password: str
    confirm: str


class PasswordChange(BaseModel):
    current: str = Field("", description="Not needed when a change is being forced")
    password: str
    confirm: str


class UserCreate(BaseModel):
    username: str = Field(min_length=1)
    display_name: str = ""
    password: str = Field(description="Temporary; the user must change it at first sign-in")
    role: Role = "user"


class UserUpdate(BaseModel):
    display_name: str = ""
    role: Role = "user"
    is_active: bool = False


class PasswordReset(BaseModel):
    password: str


class UserDelete(BaseModel):
    reassign_to: OptionalId = Field(None, description="User to take over their tickets")


# ---------------------------------------------------------------- API responses

class _Row(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class UserOut(_Row):
    id: int
    username: str
    display_name: str | None
    role: Role


class TicketSummary(_Row):
    id: int
    ref: str = Field(examples=["HD-2026-0042"])
    subject: str
    status: Status
    priority: Priority
    channel: str
    category: str | None
    customer_name: str | None
    customer_email: str | None
    owner_username: str | None = None
    received_at: datetime
    due_at: datetime | None
    overdue: bool
    active_seconds: int = Field(description="SLA clock: time spent in an active status")


class EventOut(_Row):
    id: int
    type: str
    body: str | None
    author: str | None
    user_id: int | None
    occurred_at: datetime | None
    voided: bool


class TicketDetail(TicketSummary):
    body: str | None
    subcategory: str | None
    resolution: str | None
    root_cause: str | None
    tags: list[str]
    events: list[EventOut]
