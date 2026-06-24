import logging
import os
import json
import re
import anthropic
from dotenv import load_dotenv
from openai import AzureOpenAI
from azure.search.documents import SearchClient
from azure.search.documents.models import VectorizedQuery
from azure.core.credentials import AzureKeyCredential

load_dotenv()

logger = logging.getLogger(__name__)

# =========================
# CONFIG  (unchanged)
# =========================
SEARCH_ENDPOINT = os.getenv("AZURE_SEARCH_ENDPOINT")
SEARCH_KEY      = os.getenv("AZURE_SEARCH_KEY")
INDEX_NAME      = os.getenv("AZURE_SEARCH_INDEX")

AZURE_OPENAI_API_KEY    = os.getenv("AZURE_OPENAI_API_KEY")
AZURE_OPENAI_ENDPOINT   = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT")

CLAUDE_MODEL          = "claude-haiku-4-5"
ANTHROPIC_SECRET_NAME = os.getenv("ANTHROPIC_SECRET_NAME", "caizin/anthropic-api-key")
AWS_REGION            = os.getenv("AWS_REGION")  # boto3 falls back to instance metadata if None

# =========================
# CLIENTS  (lazy — created on first use so module import never blocks)
# =========================
_search_client    = None
_azure_client     = None
_anthropic_client = None


def _get_anthropic_api_key() -> str:
    """Resolve the Anthropic API key.

    Priority:
      1. ANTHROPIC_API_KEY env var (useful for local dev)
      2. AWS Secrets Manager — JSON secret with an "ANTHROPIC_API_KEY" field
    """
    env_key = os.getenv("ANTHROPIC_API_KEY")
    if env_key:
        return env_key

    import boto3
    client = boto3.client("secretsmanager", region_name=AWS_REGION)
    response = client.get_secret_value(SecretId=ANTHROPIC_SECRET_NAME)
    data = json.loads(response["SecretString"])
    return data["ANTHROPIC_API_KEY"]

def _get_search_client():
    global _search_client
    if _search_client is None:
        _search_client = SearchClient(
            endpoint=SEARCH_ENDPOINT,
            index_name=INDEX_NAME,
            credential=AzureKeyCredential(SEARCH_KEY),
        )
    return _search_client

def _get_azure_client():
    global _azure_client
    if _azure_client is None:
        _azure_client = AzureOpenAI(
            api_key=AZURE_OPENAI_API_KEY,
            azure_endpoint=AZURE_OPENAI_ENDPOINT,
            api_version="2024-02-15-preview"
        )
    return _azure_client

def _get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.Anthropic(api_key=_get_anthropic_api_key())
    return _anthropic_client

# =========================
# EMBEDDING  (unchanged)
# =========================
def get_query_embedding(text):
    response = _get_azure_client().embeddings.create(
        model=AZURE_OPENAI_DEPLOYMENT,
        input=text
    )
    return response.data[0].embedding

# =========================
# HYBRID SEARCH  (unchanged)
# =========================
def search_documents(query: str):
    query_embedding = get_query_embedding(query)

    vector_query = VectorizedQuery(
        vector=query_embedding,
        k_nearest_neighbors=15,
        fields="embedding"
    )

    results = _get_search_client().search(
        search_text=query,
        vector_queries=[vector_query],
        top=15
    )

    docs         = []
    seen         = set()
    all_sources  = {}   # name -> url for every retrieved doc
    chunk_sources = []  # policy_name per chunk, in same order as docs

    for r in results:
        content     = r.get("content")
        policy_name = r.get("policy_name")
        policy_url  = r.get("policy_url")

        if content and content not in seen:
            docs.append(content)
            seen.add(content)
            chunk_sources.append(policy_name)

        if policy_name and policy_url:
            all_sources[policy_name] = policy_url

    return docs[:15], all_sources, chunk_sources[:15]

# =========================
# INTENT CLASSIFIER
# =========================
def _classify_intent(question: str) -> str:
    """
    Classify intent into one of five categories:
      - 'greeting'     : casual small talk
      - 'list_policies': user wants to see all available policies
      - 'hr_action'    : live HRMS action (leave balance, apply/view/cancel leave)
      - 'rag'          : policy knowledge question
      - 'other'        : off-topic, gibberish, slogans, or anything unrelated to HR/policies

    Falls back to 'other' on any error.
    """
    try:
        response = _get_anthropic_client().messages.create(
            model=CLAUDE_MODEL,
            max_tokens=20,
            temperature=0,
            messages=[{
                "role": "user",
                "content": (
                    "Classify the message below into exactly one category:\n"
                    "- \"greeting\" — a message that is ONLY a greeting or farewell with zero other content "
                    "(e.g. hi, hello, hey, good morning, thanks, bye, good night). "
                    "Do NOT classify as greeting if the message contains any question, statement, "
                    "opinion, slogan, or non-greeting content, even if it is short or casual.\n"
                    "- \"list_policies\" — user wants to see all or available company policies "
                    "(e.g. list all policies, what policies do you have, show me all docs)\n"
                    "- \"hr_action\" — a live HR system action: checking leave balance, "
                    "applying/cancelling leave, viewing leave requests or history "
                    "(e.g. 'what is my leave balance', 'apply casual leave from June 10 to 12', "
                    "'show my leaves', 'cancel my leave')\n"
                    "- \"rag\" — a genuine question about Caizin company policy rules, eligibility, "
                    "or entitlements (e.g. 'what is the leave policy', 'how many sick days am I entitled to', "
                    "'tell me about travel policy')\n"
                    "- \"other\" — anything that does not fit the above: random text, gibberish, "
                    "political slogans, non-English phrases unrelated to HR, jokes, or any message "
                    "that has nothing to do with company policies or HR actions "
                    "(e.g. 'lets make america great again', 'ab ki baar modi sarkaar', 'dbAKJ')\n\n"
                    "IMPORTANT: If the message contains BOTH a greeting AND a policy question "
                    "classify as the non-greeting category.\n\n"
                    f"Message: {question}\n\n"
                    "Reply with only the single category word."
                )
            }],
        )
        intent = next((b.text for b in response.content if b.type == "text"), "").strip().lower()
        return intent if intent in ("greeting", "list_policies", "hr_action", "rag", "other") else "other"
    except Exception as e:
        print(f"[intent] classifier failed, defaulting to other: {e}")
        return "other"


# =========================
# LEAVE INTENT EXTRACTOR
# =========================
def extract_leave_request(text: str, today: str, leave_type_names: list = None) -> dict | None:
    """
    Detect and extract leave intent from natural language in one LLM call.
    Returns:
      {"action": "apply_leave", "from_date": "YYYY-MM-DD"|null,
       "to_date": "YYYY-MM-DD"|null, "session_type": "full_day", "reason": ""}
      {"action": "check_balance"}
      None — if the message is not a leave action.
    """
    try:
        response = _get_anthropic_client().messages.create(
            model=CLAUDE_MODEL,
            max_tokens=150,
            temperature=0,
            messages=[{
                "role": "user",
                "content": (
                    f"Today is {today}. Analyze this message and reply with JSON only, no explanation.\n\n"
                    f'Message: "{text}"\n\n'
                    "Rules:\n"
                    '- Checking/viewing leave balance → {"action":"check_balance"}\n'
                    '- Applying/requesting/taking leave → {"action":"apply_leave",'
                    '"from_date":"YYYY-MM-DD or null","to_date":"YYYY-MM-DD or null",'
                    '"session_type":"full_day","reason":"","leave_type_hint":""}\n'
                    "  session_type: full_day | first_half | second_half\n"
                    "  leave_type_hint: pick the closest match from this list if a leave type is mentioned, else empty string.\n"
                    f"  Available leave types: {', '.join(leave_type_names) if leave_type_names else 'casual, sick, annual, earned'}\n"
                    "  If only one date is mentioned, set both from_date and to_date to that date.\n"
                    '- Neither → {"action":null}'
                ),
            }],
        )
        raw = next((b.text for b in response.content if b.type == "text"), "").strip()
        # strip markdown code fences if the model wrapped the JSON
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-z]*\n?", "", raw).rstrip("`").strip()
        logger.info("[extract_leave_request] raw=%s", raw)
        data = json.loads(raw)
        action = data.get("action")
        logger.info("[extract_leave_request] action=%s", action)
        return data if action else None
    except Exception as e:
        logger.info("[extract_leave_request] failed: %s", e)
        return None


# =========================
# DYNAMIC POLICY LIST
# =========================
def list_all_policies():
    """Fetch all unique policy names live from the Azure Search index."""
    client = _get_search_client()

    results = client.search(
        search_text="*",
        select=["policy_name", "policy_url"],
        top=1000
    )

    seen = {}
    for r in results:
        name = r.get("policy_name")
        url  = r.get("policy_url")
        if name and name not in seen:
            seen[name] = url

    if not seen:
        return "No policies found. Please contact HR."

    lines = ["📋 **Here are all the company policies available:**\n"]
    for i, (name, url) in enumerate(seen.items(), 1):
        if url:
            lines.append(f"{i}. [{name}]({url})")
        else:
            lines.append(f"{i}. {name}")

    lines.append("\n_Ask me about any specific policy for details._")
    lines.append("\n---\n_For final confirmation, please verify with HR._")
    return "\n".join(lines)

# =========================
# MISTRAL GENERATION  (unchanged)
# =========================
SYSTEM_PROMPT = """You are an internal Caizin company policy assistant.

CRITICAL RULES:
- Answer ONLY using the provided context.
- Do NOT assume eligibility.
- When a policy defines an explicit list (e.g. spouse, parent, child, sibling):
  - Treat the list as CLOSED.
- If the user is NOT eligible for a specific leave:
  - Clearly state the ineligibility and the reason.
- SITUATION-BASED QUESTIONS (e.g. "which leave for a wedding / travel / personal work / death of someone"):
  - Read the user's situation carefully.
  - ALWAYS check eligibility BEFORE recommending any leave type.
  - If the relation or situation qualifies → recommend that leave.
  - If the relation or situation does NOT qualify → state ineligibility FIRST, then suggest alternatives.
  - NEVER recommend a leave type and declare ineligibility for it in the same answer.
  - Recommend ONLY leave types that are genuinely applicable to that situation.
  - NEVER mention a leave type that is unrelated to the situation.
    Examples: do NOT mention Bereavement Leave for a wedding. Do NOT mention Sick Leave for a vacation.
  - Do NOT explain why unrelated leave types are ineligible — simply do not bring them up at all.
- HANDLING INELIGIBILITY:
   - If the user is NOT eligible, clearly state the reason based on the text.
   - IF AND ONLY IF the user asked about a specific "Leave Type" (like Sick Leave), you may list other available leave types.
   - IF the user asked about "Benefits" or "Reimbursements" (like Gym/Fitness), DO NOT list leave types. Only mention alternative financial benefits if they exist in the text.
- Do NOT invent leave categories.
- For numeric values, copy them EXACTLY as written.
- If no alternatives are mentioned in the policy, state that explicitly."""


def generate_answer(question: str, context_docs: list, chunk_sources: list):
    if not context_docs:
        return "I couldn't find this in the company policy.", set()

    context = "\n\n".join(context_docs)

    user_message = f"""Context:
{context}

Question:
{question}"""

    response = _get_anthropic_client().messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    answer = next((b.text for b in response.content if b.type == "text"), "")
    used_policies = set(s for s in chunk_sources if s)
    return answer, used_policies

# =========================
# MAIN QUERY ROUTER
# =========================
async def ask_policy_question(question: str, employee_email: str = "", policy_only: bool = False):
    from keka.mcp_agent import ask_keka_mcp

    intent = _classify_intent(question)

    # 1. Greeting
    if intent == "greeting":
        return "Hi there! 👋 How can I help you today?", intent

    # 2. Off-topic / gibberish — short-circuit before RAG
    if intent == "other":
        return (
            "I can only answer questions about Caizin's company policies — "
            "such as leave, travel expenses, fitness reimbursement, POSH, or anti-bribery. "
            "Please try asking a policy-related question.",
            intent,
        )

    # 3. List all policies
    if intent == "list_policies":
        return list_all_policies(), intent

    # 4. Live HR action — skip when policy_only=True (AskCAI tab serves policy docs only)
    if intent == "hr_action" and not policy_only:
        return await ask_keka_mcp(question, employee_email, _get_anthropic_api_key()), intent

    # 5. RAG pipeline — policy knowledge question
    docs, all_sources, chunk_sources = search_documents(question)
    answer, used_policies = generate_answer(question, docs, chunk_sources)

    # Only append sources + disclaimer if Mistral didn't refuse the question
    out_of_scope_phrases = [
        "does not contain any information",
        "cannot answer this question",
        "not found in the company policy",
        "no information about this",
        "outside the scope of",
        "not available in the provided",
        "context does not provide",
        "provided context does not",
        "i couldn't find this in the company policy"
    ]
    answer_lower = answer.lower()
    is_policy_answer = not any(phrase in answer_lower for phrase in out_of_scope_phrases)

    # Only show links for policies whose chunks were actually used
    relevant_sources = {name: url for name, url in all_sources.items() if name in used_policies}

    if relevant_sources and is_policy_answer:
        first_policy = next(iter(relevant_sources.items()))
        policy_name, policy_url = first_policy
        answer += (
            "\n\n---\n"
            f"📎 **View Full Policy:** [{policy_name}]({policy_url})\n"
        )

    if is_policy_answer:
        answer += (
            "\n---\n"
            "_For final confirmation and official applicability, "
            "please verify the policy details with HR._"
        )

    return answer, intent


if __name__ == "__main__":
    import asyncio
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    question = input("Ask a policy question: ")
    print("\nAnswer:\n")
    print(asyncio.run(ask_policy_question(question)))
