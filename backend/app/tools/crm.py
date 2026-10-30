"""Mock CRM / student-account lookup tool.

Stands in for a real Student Information System. Deliberately a small
in-memory dict — the assignment is graph architecture and control, not
retrieval quality (real data sources are out of scope for this phase).
"""

from langchain_core.tools import tool

_MOCK_STUDENTS = {
    "S1001": {
        "name": "Alex Chen",
        "status": "active",
        "devices": ["MacBook Pro (asset #4471)"],
        "account_locked": False,
    },
    "S1002": {
        "name": "Priya Nair",
        "status": "active",
        "devices": ["Dell Latitude (asset #2210)"],
        "account_locked": True,
    },
    "S1003": {
        "name": "Jordan Lee",
        "status": "alumni",
        "devices": [],
        "account_locked": False,
    },
}


@tool
def lookup_student_account(student_id: str) -> dict:
    """Look up a student's IT account record by student ID (mock CRM).

    Returns the student's name, enrollment status (active/alumni),
    registered devices, and whether their account is currently locked.
    If the student_id is not found, returns {"found": False}.
    """
    record = _MOCK_STUDENTS.get(student_id.upper())
    if record is None:
        return {"found": False, "student_id": student_id}
    return {"found": True, "student_id": student_id.upper(), **record}
