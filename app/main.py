from datetime import date, datetime, timedelta, timezone
from math import asin, cos, radians, sin, sqrt
from typing import NoReturn

import jwt
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import get_settings
from .db import Base, engine, ensure_additive_schema, get_db
from .models import (
    AttendanceRecord,
    AttendanceSession,
    AbsenceRequest,
    AuditLog,
    CodeAttempt,
    CourseSection,
    Enrollment,
    Notification,
    SessionStatus,
    User,
    as_utc,
)
from .schemas import (
    AttendanceConfirmRequest,
    AttendanceResponse,
    AttendanceSessionCreate,
    AttendanceSessionResponse,
    AbsenceRequestCreate,
    AbsenceRequestResponse,
    AbsenceReview,
    ClassStatsResponse,
    CodeRotationResponse,
    EnrollmentCreate,
    QrTokenResponse,
    SectionCreate,
    SectionResponse,
    StudentStatsResponse,
    NotificationResponse,
    TokenResponse,
    UserCreate,
    UserResponse,
)
from .security import (
    code_matches,
    create_token,
    current_user,
    digest_code,
    hash_password,
    make_code,
    make_qr_token,
    require_role,
    verify_password,
)

# Attempts with these results count toward the rate limits.
FAILED_RESULTS = ("invalid", "not_enrolled")

app = FastAPI(
    title="Attendance API",
    description="Secure code/QR attendance for professors and students.",
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
)


def distance_meters(latitude_a: float, longitude_a: float, latitude_b: float, longitude_b: float) -> float:
    earth_radius = 6_371_000
    lat_delta = radians(latitude_b - latitude_a)
    lon_delta = radians(longitude_b - longitude_a)
    value = sin(lat_delta / 2) ** 2 + cos(radians(latitude_a)) * cos(radians(latitude_b)) * sin(lon_delta / 2) ** 2
    return earth_radius * 2 * asin(sqrt(value))


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def ensure_open(session: AttendanceSession) -> None:
    if session.status != SessionStatus.OPEN:
        raise HTTPException(status_code=409, detail="Attendance session is not open")
    if datetime.now(timezone.utc) >= as_utc(session.expires_at):
        raise HTTPException(status_code=410, detail="Attendance session has expired")


def attempt_limit_reached(db: Session, session: AttendanceSession, student_id: str, ip: str, device_id: str | None) -> bool:
    """True when failed attempts for this student, IP, or device exceed the window limit in this session."""
    settings = get_settings()
    window_start = datetime.now(timezone.utc) - timedelta(minutes=settings.attempt_window_minutes)
    scopes = [
        (CodeAttempt.student_id == student_id, settings.attempt_limit_per_student),
        (CodeAttempt.ip_address == ip, settings.attempt_limit_per_ip),
    ]
    if device_id:
        scopes.append((CodeAttempt.device_id == device_id, settings.attempt_limit_per_device))
    for condition, limit in scopes:
        failures = db.scalar(
            select(func.count(CodeAttempt.id)).where(
                CodeAttempt.attendance_session_id == session.id,
                CodeAttempt.attempted_at >= window_start,
                CodeAttempt.result.in_(FAILED_RESULTS),
                condition,
            )
        )
        if failures >= limit:
            return True
    return False


def reject_attempt(
    db: Session,
    session: AttendanceSession,
    student_id: str,
    result: str,
    ip: str,
    device_id: str | None,
    status_code: int,
    detail: str,
) -> NoReturn:
    """Log a rejected confirmation attempt, then return the error to the client."""
    db.add(
        CodeAttempt(
            attendance_session_id=session.id,
            student_id=student_id,
            result=result,
            ip_address=ip,
            device_id=device_id,
        )
    )
    db.commit()
    raise HTTPException(status_code=status_code, detail=detail)


def qr_nonce_for(token: str, session: AttendanceSession) -> str | None:
    """Return the token's one-time nonce if it is a valid, unexpired QR token for the current code version."""
    try:
        claims = jwt.decode(token, get_settings().code_secret, algorithms=["HS256"])
    except jwt.InvalidTokenError:
        return None
    if claims.get("session_id") != session.id or claims.get("code_version") != session.code_version:
        return None
    nonce = claims.get("nonce")
    return nonce if isinstance(nonce, str) and nonce else None


@app.on_event("startup")
def create_tables() -> None:
    Base.metadata.create_all(bind=engine)
    ensure_additive_schema()


@app.get("/health", tags=["system"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/v1/auth/register", response_model=UserResponse, status_code=201, tags=["auth"])
def register(payload: UserCreate, db: Session = Depends(get_db)) -> User:
    if db.scalar(select(User).where(User.email == payload.email.lower())):
        raise HTTPException(status_code=409, detail="Email already registered")
    user = User(
        email=payload.email.lower(),
        password_hash=hash_password(payload.password),
        first_name=payload.first_name,
        last_name=payload.last_name,
        role=payload.role,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@app.post("/api/v1/auth/token", response_model=TokenResponse, tags=["auth"])
def token(form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)) -> TokenResponse:
    user = db.scalar(select(User).where(User.email == form.username.lower()))
    if user is None or not verify_password(form.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Incorrect email or password")
    return TokenResponse(access_token=create_token(user))


@app.post("/api/v1/sections", response_model=SectionResponse, status_code=201, tags=["professor"])
def create_section(
    payload: SectionCreate,
    professor: User = Depends(require_role("professor")),
    db: Session = Depends(get_db),
) -> CourseSection:
    section = CourseSection(name=payload.name, professor_id=professor.id)
    db.add(section)
    db.commit()
    db.refresh(section)
    return section


@app.post("/api/v1/sections/{section_id}/enrollments", status_code=201, tags=["professor"])
def enroll_student(
    section_id: str,
    payload: EnrollmentCreate,
    professor: User = Depends(require_role("professor")),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    section = db.get(CourseSection, section_id)
    student = db.get(User, payload.student_id)
    if section is None or section.professor_id != professor.id:
        raise HTTPException(status_code=404, detail="Section not found")
    if student is None or student.role != "student":
        raise HTTPException(status_code=404, detail="Student not found")
    enrollment = Enrollment(section_id=section_id, student_id=student.id)
    db.add(enrollment)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Student is already enrolled") from exc
    return {"status": "enrolled"}


@app.post(
    "/api/v1/attendance-sessions",
    response_model=AttendanceSessionResponse,
    status_code=201,
    tags=["professor"],
)
def open_attendance(
    payload: AttendanceSessionCreate,
    professor: User = Depends(require_role("professor")),
    db: Session = Depends(get_db),
) -> AttendanceSessionResponse:
    section = db.get(CourseSection, payload.section_id)
    if section is None or section.professor_id != professor.id:
        raise HTTPException(status_code=404, detail="Section not found")
    location_values = (payload.location_latitude, payload.location_longitude, payload.allowed_radius_meters)
    if any(value is not None for value in location_values) and not all(value is not None for value in location_values):
        raise HTTPException(status_code=422, detail="Provide latitude, longitude, and radius together")
    code = make_code()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=payload.duration_minutes)
    session = AttendanceSession(
        section_id=section.id,
        opened_by=professor.id,
        session_date=date.today(),
        expires_at=expires_at,
        code_hash=digest_code(code),
        location_latitude=payload.location_latitude,
        location_longitude=payload.location_longitude,
        allowed_radius_meters=payload.allowed_radius_meters,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    qr_token, _ = make_qr_token(session.id, session.code_version, expires_at)
    return AttendanceSessionResponse(
        id=session.id,
        section_id=session.section_id,
        expires_at=expires_at,
        code=code,
        qr_token=qr_token,
        location_required=session.location_latitude is not None,
    )


@app.post(
    "/api/v1/attendance-sessions/{session_id}/qr-token",
    response_model=QrTokenResponse,
    tags=["professor"],
)
def refresh_qr_token(
    session_id: str,
    professor: User = Depends(require_role("professor")),
    db: Session = Depends(get_db),
) -> QrTokenResponse:
    """Issue a fresh single-use QR token. Call again to refresh the QR shown on screen."""
    session = db.get(AttendanceSession, session_id)
    if session is None or session.opened_by != professor.id:
        raise HTTPException(status_code=404, detail="Attendance session not found")
    ensure_open(session)
    qr_token, expires_at = make_qr_token(session.id, session.code_version, as_utc(session.expires_at))
    return QrTokenResponse(qr_token=qr_token, expires_at=expires_at)


@app.post(
    "/api/v1/attendance-sessions/{session_id}/rotate-code",
    response_model=CodeRotationResponse,
    tags=["professor"],
)
def rotate_code(
    session_id: str,
    professor: User = Depends(require_role("professor")),
    db: Session = Depends(get_db),
) -> CodeRotationResponse:
    """Replace the session code. The old code and any QR tokens issued before this call stop working."""
    session = db.get(AttendanceSession, session_id)
    if session is None or session.opened_by != professor.id:
        raise HTTPException(status_code=404, detail="Attendance session not found")
    ensure_open(session)
    code = make_code()
    session.code_hash = digest_code(code)
    session.code_version += 1
    qr_token, expires_at = make_qr_token(session.id, session.code_version, as_utc(session.expires_at))
    db.add(
        AuditLog(actor_id=professor.id, action="attendance_code_rotated", entity_type="attendance_session", entity_id=session.id)
    )
    db.commit()
    db.refresh(session)
    return CodeRotationResponse(
        code=code,
        code_version=session.code_version,
        qr_token=qr_token,
        expires_at=expires_at,
    )


@app.post("/api/v1/attendance-sessions/{session_id}/confirm", response_model=AttendanceResponse, tags=["student"])
def confirm_attendance(
    session_id: str,
    payload: AttendanceConfirmRequest,
    request: Request,
    student: User = Depends(require_role("student")),
    db: Session = Depends(get_db),
    device_id: str | None = Header(default=None, alias="X-Device-Id", max_length=128),
) -> AttendanceResponse:
    if not payload.code and not payload.qr_token:
        raise HTTPException(status_code=422, detail="Provide code or qr_token")
    session = db.get(AttendanceSession, session_id)
    if session is None or session.status != SessionStatus.OPEN:
        raise HTTPException(status_code=404, detail="Attendance session is not open")
    if datetime.now(timezone.utc) >= as_utc(session.expires_at):
        raise HTTPException(status_code=410, detail="Attendance session has expired")
    ip = client_ip(request)
    if attempt_limit_reached(db, session, student.id, ip, device_id):
        reject_attempt(
            db, session, student.id, "rate_limited", ip, device_id,
            429, "Too many failed attempts. Try again later.",
        )
    enrollment = db.scalar(
        select(Enrollment).where(
            Enrollment.section_id == session.section_id,
            Enrollment.student_id == student.id,
            Enrollment.status == "active",
        )
    )
    if enrollment is None:
        reject_attempt(db, session, student.id, "not_enrolled", ip, device_id, 403, "Student is not enrolled")
    code_ok = bool(payload.code and code_matches(payload.code, session.code_hash))
    qr_nonce = qr_nonce_for(payload.qr_token, session) if payload.qr_token else None
    if not code_ok and qr_nonce is None:
        reject_attempt(db, session, student.id, "invalid", ip, device_id, 400, "Invalid attendance code or QR")
    # A valid QR is single-use: its nonce may appear on one record only.
    method = "qr" if qr_nonce else "code"
    if qr_nonce and db.scalar(select(AttendanceRecord.id).where(AttendanceRecord.qr_nonce == qr_nonce)):
        reject_attempt(db, session, student.id, "invalid", ip, device_id, 409, "QR code has already been used")
    location_required = session.location_latitude is not None
    if location_required and (payload.latitude is None or payload.longitude is None):
        raise HTTPException(status_code=422, detail="Location is required for this attendance session")
    if location_required and distance_meters(
        session.location_latitude,
        session.location_longitude,
        payload.latitude,
        payload.longitude,
    ) > session.allowed_radius_meters:
        raise HTTPException(status_code=403, detail="You are outside the attendance location")
    # One device may confirm attendance for only one student per session.
    if device_id and db.scalar(
        select(AttendanceRecord.id).where(
            AttendanceRecord.attendance_session_id == session.id,
            AttendanceRecord.device_id == device_id,
            AttendanceRecord.student_id != student.id,
        )
    ):
        reject_attempt(
            db, session, student.id, "device_conflict", ip, device_id,
            409, "This device already confirmed attendance for another student",
        )
    record = AttendanceRecord(
        attendance_session_id=session.id,
        student_id=student.id,
        confirmation_method=method,
        latitude=payload.latitude,
        longitude=payload.longitude,
        qr_nonce=qr_nonce,
        ip_address=ip,
        device_id=device_id,
    )
    db.add(record)
    db.add(
        CodeAttempt(
            attendance_session_id=session.id,
            student_id=student.id,
            result="accepted",
            ip_address=ip,
            device_id=device_id,
        )
    )
    db.add(AuditLog(actor_id=student.id, action="attendance_confirmed", entity_type="attendance_session", entity_id=session.id))
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Attendance already confirmed") from exc
    db.refresh(record)
    return AttendanceResponse(
        id=record.id,
        session_id=record.attendance_session_id,
        status=record.status,
        confirmation_method=record.confirmation_method,
        confirmed_at=record.confirmed_at,
    )


@app.post("/api/v1/attendance-sessions/{session_id}/cancel", tags=["professor"])
def cancel_attendance(
    session_id: str,
    professor: User = Depends(require_role("professor")),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    session = db.get(AttendanceSession, session_id)
    if session is None or session.opened_by != professor.id:
        raise HTTPException(status_code=404, detail="Attendance session not found")
    if session.status != SessionStatus.OPEN:
        raise HTTPException(status_code=409, detail="Attendance session is already closed")
    session.status = SessionStatus.CANCELLED
    student_ids = db.scalars(
        select(Enrollment.student_id).where(Enrollment.section_id == session.section_id, Enrollment.status == "active")
    ).all()
    for student_id in student_ids:
        db.add(
            Notification(
                user_id=student_id,
                title="Class attendance cancelled",
                message=f"Attendance for {session.session_date.isoformat()} was cancelled by your professor.",
            )
        )
    db.add(AuditLog(actor_id=professor.id, action="attendance_cancelled", entity_type="attendance_session", entity_id=session.id))
    db.commit()
    return {"status": "cancelled", "notifications_sent": str(len(student_ids))}


@app.post("/api/v1/absence-requests", response_model=AbsenceRequestResponse, status_code=201, tags=["absence"])
def request_absence(
    payload: AbsenceRequestCreate,
    student: User = Depends(require_role("student")),
    db: Session = Depends(get_db),
) -> AbsenceRequest:
    enrollment = db.scalar(
        select(Enrollment).where(
            Enrollment.section_id == payload.section_id,
            Enrollment.student_id == student.id,
            Enrollment.status == "active",
        )
    )
    if enrollment is None:
        raise HTTPException(status_code=403, detail="Student is not enrolled")
    if payload.attendance_session_id:
        session = db.get(AttendanceSession, payload.attendance_session_id)
        if session is None or session.section_id != payload.section_id:
            raise HTTPException(status_code=404, detail="Attendance session not found")
    absence = AbsenceRequest(
        student_id=student.id,
        section_id=payload.section_id,
        attendance_session_id=payload.attendance_session_id,
        reason=payload.reason,
        attachment_url=payload.attachment_url,
    )
    db.add(absence)
    db.commit()
    db.refresh(absence)
    return absence


@app.get(
    "/api/v1/sections/{section_id}/absence-requests",
    response_model=list[AbsenceRequestResponse],
    tags=["absence"],
)
def section_absence_requests(
    section_id: str,
    professor: User = Depends(require_role("professor")),
    db: Session = Depends(get_db),
) -> list[AbsenceRequest]:
    section = db.get(CourseSection, section_id)
    if section is None or section.professor_id != professor.id:
        raise HTTPException(status_code=404, detail="Section not found")
    return db.scalars(
        select(AbsenceRequest)
        .where(AbsenceRequest.section_id == section_id)
        .order_by(AbsenceRequest.created_at.desc())
    ).all()


@app.patch("/api/v1/absence-requests/{request_id}", response_model=AbsenceRequestResponse, tags=["absence"])
def review_absence(
    request_id: str,
    payload: AbsenceReview,
    professor: User = Depends(require_role("professor")),
    db: Session = Depends(get_db),
) -> AbsenceRequest:
    absence = db.get(AbsenceRequest, request_id)
    section = db.get(CourseSection, absence.section_id) if absence else None
    if absence is None or section is None or section.professor_id != professor.id:
        raise HTTPException(status_code=404, detail="Absence request not found")
    if absence.status != "pending":
        raise HTTPException(status_code=409, detail="Absence request was already reviewed")
    absence.status = payload.status
    db.add(
        Notification(
            user_id=absence.student_id,
            title="Absence request updated",
            message=f"Your absence request was {payload.status}.",
        )
    )
    db.add(AuditLog(actor_id=professor.id, action=f"absence_{payload.status}", entity_type="absence_request", entity_id=absence.id))
    db.commit()
    db.refresh(absence)
    return absence


@app.get("/api/v1/notifications", response_model=list[NotificationResponse], tags=["notifications"])
def notifications(
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[Notification]:
    return db.scalars(
        select(Notification).where(Notification.user_id == user.id).order_by(Notification.created_at.desc()).limit(100)
    ).all()


@app.get("/api/v1/students/me/statistics", response_model=StudentStatsResponse, tags=["statistics"])
def student_statistics(
    student: User = Depends(require_role("student")),
    db: Session = Depends(get_db),
) -> StudentStatsResponse:
    sessions = db.scalars(
        select(AttendanceSession)
        .join(Enrollment, Enrollment.section_id == AttendanceSession.section_id)
        .where(Enrollment.student_id == student.id, Enrollment.status == "active")
        .order_by(AttendanceSession.session_date)
    ).all()
    records = {
        record.attendance_session_id: record
        for record in db.scalars(select(AttendanceRecord).where(AttendanceRecord.student_id == student.id)).all()
    }
    present = sum(1 for record in records.values() if record.status == "present")
    late = sum(1 for record in records.values() if record.status == "late")
    by_session = [
        {
            "session_id": session.id,
            "date": session.session_date.isoformat(),
            "status": records[session.id].status if session.id in records else "absent",
        }
        for session in sessions
    ]
    total = len(sessions)
    attended = present + late
    return StudentStatsResponse(
        student_id=student.id,
        total_sessions=total,
        attended_sessions=attended,
        attendance_percentage=round(attended * 100 / total, 2) if total else 0,
        present=present,
        late=late,
        absent=total - attended,
        by_session=by_session,
    )


@app.get("/api/v1/sections/{section_id}/statistics", response_model=ClassStatsResponse, tags=["statistics"])
def class_statistics(
    section_id: str,
    professor: User = Depends(require_role("professor")),
    db: Session = Depends(get_db),
) -> ClassStatsResponse:
    section = db.get(CourseSection, section_id)
    if section is None or section.professor_id != professor.id:
        raise HTTPException(status_code=404, detail="Section not found")
    sessions = db.scalars(
        select(AttendanceSession)
        .where(AttendanceSession.section_id == section_id)
        .order_by(AttendanceSession.session_date)
    ).all()
    enrolled_count = len(
        db.scalars(select(Enrollment).where(Enrollment.section_id == section_id, Enrollment.status == "active")).all()
    )
    records = db.scalars(
        select(AttendanceRecord).join(AttendanceSession).where(AttendanceSession.section_id == section_id)
    ).all()
    records_by_session: dict[str, int] = {}
    for record in records:
        records_by_session[record.attendance_session_id] = records_by_session.get(record.attendance_session_id, 0) + 1
    total_possible = len(sessions) * enrolled_count
    by_session = [
        {
            "session_id": session.id,
            "date": session.session_date.isoformat(),
            "confirmed": records_by_session.get(session.id, 0),
            "enrolled": enrolled_count,
            "attendance_percentage": round(records_by_session.get(session.id, 0) * 100 / enrolled_count, 2)
            if enrolled_count
            else 0,
        }
        for session in sessions
    ]
    return ClassStatsResponse(
        section_id=section_id,
        total_sessions=len(sessions),
        enrolled_students=enrolled_count,
        total_confirmations=len(records),
        attendance_percentage=round(len(records) * 100 / total_possible, 2) if total_possible else 0,
        by_session=by_session,
    )
