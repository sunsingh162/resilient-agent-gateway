"""Mock knowledge-base search tool for IT troubleshooting articles."""

from langchain_core.tools import tool

_KB_ARTICLES = [
    {
        "id": "KB-101",
        "category": "password",
        "title": "Resetting your student portal password",
        "body": (
            "Go to portal.university.edu/reset, enter your student email, and "
            "follow the emailed link. Resets take effect within 5 minutes."
        ),
    },
    {
        "id": "KB-102",
        "category": "network",
        "title": "Fixing intermittent Wi-Fi drops on campus",
        "body": (
            "Forget and rejoin the 'CampusSecure' network, and disable Wi-Fi "
            "power-saving mode in your device's network settings."
        ),
    },
    {
        "id": "KB-103",
        "category": "hardware",
        "title": "Reporting broken lab or university-owned equipment",
        "body": (
            "Note the asset tag on the device and the building/room, then log a "
            "hardware ticket so facilities can dispatch a technician."
        ),
    },
    {
        "id": "KB-104",
        "category": "account",
        "title": "Requesting account deactivation after graduation",
        "body": (
            "Account deactivation requests require identity verification and are "
            "processed by the registrar's office within 3 business days."
        ),
    },
]


@tool
def search_kb(query: str) -> list[dict]:
    """Search the IT knowledge base for troubleshooting articles relevant to a query.

    Returns a list of matching articles (id, category, title, body). Returns
    an empty list if nothing matches — callers should not assume a hit.
    """
    query_words = set(query.lower().split())
    matches = [
        article
        for article in _KB_ARTICLES
        if article["category"] in query.lower()
        or query_words & set(article["title"].lower().split())
        or query_words & set(article["body"].lower().split())
    ]
    return matches
