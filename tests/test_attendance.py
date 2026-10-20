from datetime import datetime, timedelta, timezone

from app.models import AttendanceSession, SessionStatus
from tests.helpers import confirm, open_session

# Campus coordinates used for location tests.
HERE = {"latitude": 41.0082, "longitude": 28.9784}
NEARBY = {"latitude": 41.0083, "longitude": 28.9784}  # about 11 m north
FAR = {"latitude": 41.0500, "longitude": 28.9784}  # about 4.9 km north


def test_open_session_returns_code_qr_and_expiry(client, professor, section):
    body = open_session(client, professor, section, duration_minutes=15)
    assert body["code"]
    assert body["qr_token"]
    assert body["location_required"] is False
    expires = datetime.fromisoformat(body["expires_at"])
    assert timedelta(minutes=14) < expires - datetime.now(timezone.utc) <= timedelta(minutes=15, seconds=5)


def test_open_session_stores_hash_not_plain_code(client, professor, section, db):
    body = open_session(client, professor, section)
    stored = db.get(AttendanceSession, body["id"])
    assert stored.code_hash != body["code"]
    assert body["code"] not in stored.code_hash


def test_duration_outside_allowed_range_is_rejected(client, professor, section):
    for minutes in (0, 121):
        response = client.post(
            "/api/v1/attendance-sessions",
            json={"section_id": section, "duration_minutes": minutes},
            headers=professor["headers"],
        )
        assert response.status_code == 422


def test_only_owner_professor_can_open_session(client, other_professor, section):
    response = client.post(
        "/api/v1/attendance-sessions",
        json={"section_id": section},
        headers=other_professor["headers"],
    )
    assert response.status_code == 404


def test_location_fields_must_be_given_together(client, professor, section):
    response = client.post(
        "/api/v1/attendance-sessions",
        json={"section_id": section, "location_latitude": 41.0, "location_longitude": 28.9},
        headers=professor["headers"],
    )
    assert response.status_code == 422


def test_student_confirms_with_code(client, professor, section, student):
    body = open_session(client, professor, section)
    response = confirm(client, student, body["id"], code=body["code"])
    assert response.status_code == 200
    assert response.json()["confirmation_method"] == "code"
    assert response.json()["status"] == "present"


def test_wrong_code_is_rejected(client, professor, section, student):
    body = open_session(client, professor, section)
    response = confirm(client, student, body["id"], code="WRONGCODE12")
    assert response.status_code == 400


def test_confirming_twice_is_a_conflict(client, professor, section, student):
    body = open_session(client, professor, section)
    assert confirm(client, student, body["id"], code=body["code"]).status_code == 200
    assert confirm(client, student, body["id"], code=body["code"]).status_code == 409


def test_unenrolled_student_is_forbidden(client, professor, section, outsider):
    body = open_session(client, professor, section)
    response = confirm(client, outsider, body["id"], code=body["code"])
    assert response.status_code == 403


def test_confirm_requires_code_or_qr(client, professor, section, student):
    body = open_session(client, professor, section)
    assert confirm(client, student, body["id"]).status_code == 422


def test_code_is_shared_by_all_enrolled_students(client, professor, section, student, other_student):
    body = open_session(client, professor, section)
    assert confirm(client, student, body["id"], code=body["code"]).status_code == 200
    assert confirm(client, other_student, body["id"], code=body["code"]).status_code == 200


def test_expired_session_returns_410(client, professor, section, student, db):
    body = open_session(client, professor, section)
    session = db.get(AttendanceSession, body["id"])
    session.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    response = confirm(client, student, body["id"], code=body["code"])
    assert response.status_code == 410


def test_cancelled_session_is_not_open(client, professor, section, student):
    body = open_session(client, professor, section)
    assert client.post(f"/api/v1/attendance-sessions/{body['id']}/cancel", headers=professor["headers"]).status_code == 200
    response = confirm(client, student, body["id"], code=body["code"])
    assert response.status_code == 404


def test_cancel_twice_is_a_conflict(client, professor, section):
    body = open_session(client, professor, section)
    client.post(f"/api/v1/attendance-sessions/{body['id']}/cancel", headers=professor["headers"])
    response = client.post(f"/api/v1/attendance-sessions/{body['id']}/cancel", headers=professor["headers"])
    assert response.status_code == 409


def test_cancel_notifies_enrolled_students(client, professor, section, student, other_student):
    body = open_session(client, professor, section)
    response = client.post(f"/api/v1/attendance-sessions/{body['id']}/cancel", headers=professor["headers"])
    assert response.json()["notifications_sent"] == "2"
    notes = client.get("/api/v1/notifications", headers=student["headers"]).json()
    assert notes[0]["title"] == "Class attendance cancelled"


def test_cancel_only_by_owner(client, other_professor, professor, section):
    body = open_session(client, professor, section)
    response = client.post(f"/api/v1/attendance-sessions/{body['id']}/cancel", headers=other_professor["headers"])
    assert response.status_code == 404


def test_location_required_and_enforced(client, professor, section, student):
    body = open_session(client, professor, section, allowed_radius_meters=100, location_latitude=HERE["latitude"], location_longitude=HERE["longitude"])
    assert body["location_required"] is True

    no_coords = confirm(client, student, body["id"], code=body["code"])
    assert no_coords.status_code == 422

    far = confirm(client, student, body["id"], code=body["code"], **FAR)
    assert far.status_code == 403

    near = confirm(client, student, body["id"], code=body["code"], **NEARBY)
    assert near.status_code == 200


def test_location_is_stored_on_the_record(client, professor, section, student, db):
    from app.models import AttendanceRecord

    body = open_session(client, professor, section, allowed_radius_meters=100, location_latitude=HERE["latitude"], location_longitude=HERE["longitude"])
    confirm(client, student, body["id"], code=body["code"], **NEARBY)
    record = db.query(AttendanceRecord).filter_by(student_id=student["id"]).one()
    assert record.latitude == NEARBY["latitude"]
    assert record.longitude == NEARBY["longitude"]


def test_attempts_are_logged(client, professor, section, student, outsider, db):
    from app.models import CodeAttempt

    body = open_session(client, professor, section)
    confirm(client, student, body["id"], code="WRONGCODE12")
    confirm(client, student, body["id"], code=body["code"])
    confirm(client, outsider, body["id"], code=body["code"])
    results = sorted(row.result for row in db.query(CodeAttempt).all())
    assert results == ["accepted", "invalid", "not_enrolled"]
