from tests.helpers import create_section


def test_professor_creates_section_linked_to_them(client, professor):
    response = client.post("/api/v1/sections", json={"name": "Algorithms"}, headers=professor["headers"])
    assert response.status_code == 201
    assert response.json()["professor_id"] == professor["id"]
    assert response.json()["name"] == "Algorithms"


def test_section_name_is_required(client, professor):
    response = client.post("/api/v1/sections", json={"name": ""}, headers=professor["headers"])
    assert response.status_code == 422


def test_enroll_student(client, professor, section, outsider):
    response = client.post(
        f"/api/v1/sections/{section}/enrollments",
        json={"student_id": outsider["id"]},
        headers=professor["headers"],
    )
    assert response.status_code == 201


def test_enrolling_same_student_twice_is_a_conflict(client, professor, section, student):
    response = client.post(
        f"/api/v1/sections/{section}/enrollments",
        json={"student_id": student["id"]},
        headers=professor["headers"],
    )
    assert response.status_code == 409


def test_cannot_enroll_into_another_professors_section(client, other_professor, section, outsider):
    response = client.post(
        f"/api/v1/sections/{section}/enrollments",
        json={"student_id": outsider["id"]},
        headers=other_professor["headers"],
    )
    assert response.status_code == 404


def test_cannot_enroll_a_professor_as_student(client, professor, section, other_professor):
    response = client.post(
        f"/api/v1/sections/{section}/enrollments",
        json={"student_id": other_professor["id"]},
        headers=professor["headers"],
    )
    assert response.status_code == 404


def test_student_cannot_enroll_anyone(client, student, section, outsider):
    response = client.post(
        f"/api/v1/sections/{section}/enrollments",
        json={"student_id": outsider["id"]},
        headers=student["headers"],
    )
    assert response.status_code == 403


def test_second_professor_can_create_their_own_section(client, other_professor):
    section_id = create_section(client, other_professor, name="Other class")
    assert section_id
