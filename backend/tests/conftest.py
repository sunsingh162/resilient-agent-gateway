"""Load .env before tests run, so tests that need ANTHROPIC_API_KEY pick it
up the same way the demo scripts and the FastAPI app do, without requiring
it to be exported manually in the shell running pytest.
"""

from dotenv import load_dotenv

load_dotenv()
