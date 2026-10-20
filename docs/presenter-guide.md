# Presenter guide: how the attendance system works

This guide describes the system as it is implemented in `app/`. Where the
planning documents in `docs/` describe something that is not built yet, this is
called out explicitly. Present the implemented behavior, and use the "Known gaps"
section if someone asks about security or missing features.

## 1. Language and framework

- **Language:** Python 3.11+ (Docker image uses Python 3.12).
- **Web framework:** FastAPI, served by Uvicorn.
- **ORM:** SQLAlchemy 2.0.
- **Configuration:** pydantic-settings, read from environment variables or `.env`.
- **Not used:** Node/Express, Spring Boot, Django, ASP.NET.

Source files:

| File | Purpose |
| --- | --- |
| `app/main.py` | All routes and business rules |
| `app/models.py` | Database tables (SQLAlchemy models) |
| `app/schemas.py` | Request and response validation (Pydantic) |
| `app/security.py` | Passwords, codes, JWT, role checks |
| `app/db.py` | Database connection and schema setup |
| `app/config.py` | Settings and secrets |

## 2. Database

- **Production / Docker:** PostgreSQL 16 (`docker-compose.yml`), via the
  `psycopg` driver.
- **Local fallback:** SQLite (`sqlite:///./attendance.db`) if `DATABASE_URL` is
  not set.
- **Schema setup:** tables are created automatically at startup
  (`Base.metadata.create_all`). There is no migration tool such as Alembic.
  `ensure_additive_schema()` adds three location columns to existing databases.

## 3. Attendance code generation

The code is created in `make_code()` (`app/security.py`):

1. `secrets.token_urlsafe(8)` generates 8 random bytes, from a
   cryptographically secure source, encoded as base64url.
2. The `-` and `_` characters are removed, the result is uppercased, and it is
   cut to **10 characters**. Occasionally it is 9 characters, when the random
   string contains `-` or `_`.
3. The alphabet is letters A–Z and digits 0–9. The effective randomness is
   about **51 bits**.

Codes are generated once per attendance session, when the professor opens it
(`POST /api/v1/attendance-sessions`). One code is shared by all students in that
session. It is not single-use per student.

**Validity:**

- Lifetime is set by `duration_minutes`: default **15**, allowed range **1–120**.
- The code stops working when `expires_at` passes (the API returns `410`), or when
  the professor cancels the session (`status = cancelled`).
- The professor can rotate the code with `POST /api/v1/attendance-sessions/{id}/rotate-code`.
  This increments `code_version`, so the old code and any QR tokens issued earlier stop working.

**Storage:** the plain code is returned once in the response to the professor.
The database stores only `code_hash`, which is an **HMAC-SHA256** of the code
keyed with `CODE_SECRET`.

**Case sensitivity:** the comparison is exact. A student who types lowercase
letters is rejected. The student UI should uppercase the input before sending it.

## 4. Stopping code sharing

| Control | Status | Detail |
| --- | --- | --- |
| Time window | Implemented | Expires after `duration_minutes`, checked on the server |
| One confirmation per student per session | Implemented | `UNIQUE(attendance_session_id, student_id)` on records. A second attempt returns `409` |
| Enrollment check | Implemented | Student must have an `active` enrollment in the section, or the API returns `403` |
| Signed QR token | Implemented | JWT (HS256, `CODE_SECRET`) with `session_id`, `code_version`, and `exp` |
| Optional location fence | Implemented, per session | Professor can set latitude, longitude, and radius (10–1000 m). The server computes the distance (haversine) and rejects students outside it (`403`) |
| Attempt logging | Implemented | Every accepted, invalid, or not-enrolled attempt is written to `attendance_code_attempts` |
| Rate limiting | Implemented | Failed attempts (`invalid`, `not_enrolled`) are counted per student, per client IP, and per device within a sliding window (default 10 min). Going over a limit returns `429` and logs a `rate_limited` attempt. Limits: 5 per student, 30 per IP, 30 per device |
| IP and device check | Implemented | Each attempt stores the client IP and an optional `X-Device-Id` header. One device can confirm for only one student per session (`409` otherwise) |
| One-time QR token | Implemented | Each QR token carries a random nonce and expires after 30 s (`QR_TTL_SECONDS`). The nonce is saved on the attendance record with a unique index, so a token works for one student only. The professor refreshes the QR with `POST .../qr-token` |
| Code rotation | Implemented | `POST .../rotate-code` issues a new code and increments `code_version`. The old code and any QR tokens issued before the rotation stop working |

**What to say:** the window is short, each student can count only once, and
enrollment is required. A QR code now works for one student and expires within
30 seconds, so a screenshot is useless once it has been used or has expired. Failed
guesses are rate-limited, and one device cannot confirm for two students. Codes
can be rotated during a session. The optional location fence is still the main
defence against someone sharing the code with students who are not in the room,
and only if the professor turns it on.

**Limits of these controls:** the IP is the address the app sees directly. Behind a
reverse proxy, every student would share the proxy's address, so the IP limit
needs proxy headers configured before production. The device ID is a header the
client sends, so a determined student can change it. These controls deter casual
sharing and guessing. They do not stop a student who is willing to work around them.

**Location caveat:** the distance check uses coordinates sent by the student's
device. GPS can be spoofed, so the fence is a deterrent, not proof of presence.

## 5. Two-factor authentication

**Not implemented.** Login is email and password only. There is no email code,
authenticator app, or SMS. Notifications are in-app only (`GET /api/v1/notifications`).

## 6. Password storage

Passwords are hashed with `pwdlib`, using `PasswordHash.recommended()`. In
pwdlib this is Argon2 (Argon2id). It is **not bcrypt**. Passwords must be at
least **12 characters** (`UserCreate` in `app/schemas.py`).

> Note: `pwdlib` was not installed in the environment where this guide was
> written, so the algorithm name was not checked by running code. Confirm it with
> `PasswordHash.recommended().hashers` after `pip install .`.

## 7. Login method

- **Token type:** JWT (HS256), sent as `Authorization: Bearer <token>`. The API
  is stateless; no server-side sessions are stored.
- **Claims:** `sub` (user ID), `role`, `exp`. Lifetime is `ACCESS_TOKEN_MINUTES`,
  default **30**.
- **Per request:** the user is loaded from the database and rejected if
  `is_active` is false. This is why a deactivated account stops working
  immediately, even with a valid token.
- **Not implemented:** refresh tokens, logout, and token revocation.
- **Login endpoint:** `POST /api/v1/auth/token`, form fields `username` (email)
  and `password`.

## 8. Main API endpoints

All paths start with `/api/v1` unless noted. "Role" is the caller's role that the
endpoint enforces.

| Method | Path | Role | Purpose |
| --- | --- | --- | --- |
| GET | `/health` | none | Health check |
| POST | `/auth/register` | none | Create a user (professor or student) |
| POST | `/auth/token` | none | Log in, returns JWT |
| POST | `/sections` | professor | Create a class section |
| POST | `/sections/{section_id}/enrollments` | professor | Enroll a student (`student_id` in body) |
| POST | `/attendance-sessions` | professor | Open attendance. Returns `code`, `qr_token`, `expires_at` |
| POST | `/attendance-sessions/{session_id}/confirm` | student | Submit `code` or `qr_token` (plus location if required) |
| POST | `/attendance-sessions/{session_id}/qr-token` | professor | Issue a fresh single-use QR token (valid 30 s). Call repeatedly to refresh the QR on screen |
| POST | `/attendance-sessions/{session_id}/rotate-code` | professor | Replace the session code. Returns the new code and a QR token. Old codes and QR tokens stop working |
| POST | `/attendance-sessions/{session_id}/cancel` | professor | Cancel an open session. Notifies enrolled students |
| POST | `/absence-requests` | student | Submit an excused-absence request |
| GET | `/sections/{section_id}/absence-requests` | professor | List requests for a section |
| PATCH | `/absence-requests/{request_id}` | professor | Approve or reject (`approved` / `rejected`) |
| GET | `/notifications` | any logged-in user | Latest 100 notifications |
| GET | `/students/me/statistics` | student | Own attendance percentage and per-session status |
| GET | `/sections/{section_id}/statistics` | professor | Class totals and per-session confirmation counts |

Interactive docs are at `/docs` (Swagger UI) and `/redoc`.

**"Get attendance":** there is no endpoint that returns the list of students who
attended a session. Professors get counts per session from the statistics
endpoint. Students get their own per-session status.

## 9. Database tables

Nine tables are implemented in `app/models.py`. All primary keys are UUIDs stored
as `VARCHAR(36)`.

**users**

| Field | Type | Notes |
| --- | --- | --- |
| id | varchar(36) | PK |
| email | varchar(320) | Unique, indexed. Stored lowercase |
| password_hash | text | Argon2 hash |
| first_name | varchar(100) | |
| last_name | varchar(100) | |
| role | varchar(20) | Indexed. `professor` or `student` |
| is_active | boolean | Default true |

**course_sections** (the "subject" / class)

| Field | Type | Notes |
| --- | --- | --- |
| id | varchar(36) | PK |
| name | varchar(200) | For example "Databases, group A" |
| professor_id | varchar(36) | FK → users.id, indexed |

**enrollments** (student in a section)

| Field | Type | Notes |
| --- | --- | --- |
| id | varchar(36) | PK |
| section_id | varchar(36) | FK → course_sections.id, indexed |
| student_id | varchar(36) | FK → users.id, indexed |
| status | varchar(20) | Default `active` |
|  |  | `UNIQUE(section_id, student_id)` |

**attendance_sessions** (one class meeting with a code)

| Field | Type | Notes |
| --- | --- | --- |
| id | varchar(36) | PK |
| section_id | varchar(36) | FK → course_sections.id, indexed |
| opened_by | varchar(36) | FK → users.id (the professor) |
| session_date | date | |
| starts_at | timestamptz | |
| expires_at | timestamptz | |
| status | varchar(20) | `open`, `closed`, or `cancelled` |
| code_hash | text | HMAC of the code. The plain code is never stored |
| code_version | integer | Default 1. Never changed |
| location_latitude | float | Optional |
| location_longitude | float | Optional |
| allowed_radius_meters | integer | Optional, 10–1000 |

**attendance_records** (successful confirmations)

| Field | Type | Notes |
| --- | --- | --- |
| id | varchar(36) | PK |
| attendance_session_id | varchar(36) | FK → attendance_sessions.id, indexed |
| student_id | varchar(36) | FK → users.id, indexed |
| status | varchar(20) | Default `present`. Only `present` is ever written; `late` is never set |
| confirmation_method | varchar(20) | `code` or `qr` |
| confirmed_at | timestamptz | |
| latitude | float | Optional, as sent by the student |
| longitude | float | Optional, as sent by the student |
|  |  | `UNIQUE(attendance_session_id, student_id)` |

**attendance_code_attempts** (audit of every submission)

| Field | Type | Notes |
| --- | --- | --- |
| id | varchar(36) | PK |
| attendance_session_id | varchar(36) | FK → attendance_sessions.id, indexed |
| student_id | varchar(36) | FK → users.id, indexed |
| result | varchar(30) | `accepted`, `invalid`, or `not_enrolled` |
| attempted_at | timestamptz | |

**audit_logs** (who did what)

| Field | Type | Notes |
| --- | --- | --- |
| id | varchar(36) | PK |
| actor_id | varchar(36) | FK → users.id, indexed |
| action | varchar(100) | For example `attendance_confirmed`, `attendance_cancelled`, `absence_approved` |
| entity_type | varchar(100) | For example `attendance_session` |
| entity_id | varchar(36) | Not a foreign key, so it can point to any table |
| created_at | timestamptz | |

**absence_requests**

| Field | Type | Notes |
| --- | --- | --- |
| id | varchar(36) | PK |
| student_id | varchar(36) | FK → users.id, indexed |
| section_id | varchar(36) | FK → course_sections.id, indexed |
| attendance_session_id | varchar(36) | FK → attendance_sessions.id, optional |
| reason | text | 10–2000 characters |
| attachment_url | text | Optional. No upload service is built |
| status | varchar(20) | Default `pending`. Then `approved` or `rejected` |
| created_at | timestamptz | |

**notifications**

| Field | Type | Notes |
| --- | --- | --- |
| id | varchar(36) | PK |
| user_id | varchar(36) | FK → users.id, indexed |
| title | varchar(200) | |
| message | text | |
| read_at | timestamptz | Optional. No endpoint sets it yet |
| created_at | timestamptz | |

## 10. How the tables link

```mermaid
erDiagram
  users ||--o{ course_sections : "teaches (professor_id)"
  users ||--o{ enrollments : "student_id"
  course_sections ||--o{ enrollments : "section_id"
  course_sections ||--o{ attendance_sessions : "section_id"
  users ||--o{ attendance_sessions : "opened_by"
  attendance_sessions ||--o{ attendance_records : "session_id"
  users ||--o{ attendance_records : "student_id"
  attendance_sessions ||--o{ attendance_code_attempts : "session_id"
  users ||--o{ attendance_code_attempts : "student_id"
  users ||--o{ audit_logs : "actor_id"
  users ||--o{ absence_requests : "student_id"
  course_sections ||--o{ absence_requests : "section_id"
  attendance_sessions |o--o{ absence_requests : "optional session_id"
  users ||--o{ notifications : "user_id"

  users {
    varchar id PK
    varchar email UK
    text password_hash
    varchar role
    boolean is_active
  }
  course_sections {
    varchar id PK
    varchar name
    varchar professor_id FK
  }
  enrollments {
    varchar id PK
    varchar section_id FK
    varchar student_id FK
    varchar status
  }
  attendance_sessions {
    varchar id PK
    varchar section_id FK
    varchar opened_by FK
    date session_date
    timestamptz expires_at
    varchar status
    text code_hash
    int code_version
  }
  attendance_records {
    varchar id PK
    varchar attendance_session_id FK
    varchar student_id FK
    varchar status
    varchar confirmation_method
    timestamptz confirmed_at
  }
  attendance_code_attempts {
    varchar id PK
    varchar attendance_session_id FK
    varchar student_id FK
    varchar result
    timestamptz attempted_at
  }
  audit_logs {
    varchar id PK
    varchar actor_id FK
    varchar action
    varchar entity_type
    varchar entity_id
  }
  absence_requests {
    varchar id PK
    varchar student_id FK
    varchar section_id FK
    varchar attendance_session_id FK
    text reason
    varchar status
  }
  notifications {
    varchar id PK
    varchar user_id FK
    varchar title
    text message
    timestamptz read_at
  }
```

Key constraints:

- `enrollments`: one row per student per section.
- `attendance_records`: one row per student per session (no double confirmation).

## 11. Roles and relationships

- **Roles** are stored as a text field, `users.role`, with the values `professor`
  and `student`. There is no separate roles table. The value is checked when a
  user registers, and each endpoint checks it with `require_role(...)`.
- **There is no admin role** in the code, although some planning docs mention one.
- **Teacher to subject:** a professor is linked to a section through
  `course_sections.professor_id`. A section is the "subject" in this design. It
  has a name but no separate course table. One professor per section.
- **Student to group:** students are linked to sections through `enrollments`.
  This is many-to-many: a student can be in several sections, and a section has
  many students. There is no separate "group" concept. A section is the group.

## 12. Known gaps (say these before someone finds them)

These are real limitations in the current code. Presenting them openly is better
than being asked about them.

1. **Open self-registration.** `POST /auth/register` lets anyone create a
   professor account. There is no email verification or admin approval.
2. **No way for a student to find a session ID.** The confirm endpoint needs
   `session_id` in the URL. The API has no endpoint that lists a student's
   sections or open sessions, so a student who types a code cannot get the ID
   from the API. Only the QR token carries it.
3. **No way for a professor to find student IDs or sections.** Enrollment needs
   a `student_id` UUID, and there is no endpoint to search students or list
   sections.
4. **No attendee list.** Professors see counts per session, not who attended.
5. **Statistics count cancelled and not-yet-confirmed sessions as absent.**
6. **`late` is never set.** Every record is `present`.
7. **Rate limits and device checks depend on the deployment.** The IP limit
   needs proxy headers configured behind a reverse proxy, and the device ID is
   client-supplied.
8. **Location relies on client-reported GPS.**
9. **No 2FA, refresh tokens, or logout.**
10. **No test suite is tracked in the repository,** although pytest is listed as a
    dev dependency.
11. **No migrations.** Schema changes depend on `create_all` and the small
    `ensure_additive_schema()` helper.
12. **Planned design is not built.** `docs/attendance-database.md` and
    `docs/attendance-database.puml` also describe `departments`, `courses`,
    `student_profiles`, `professor_profiles`, and `class_meetings`. None of these
    exist in `app/models.py`. Present the tables in section 9 as the real schema.
