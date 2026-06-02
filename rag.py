import os
import json
import anthropic
from dotenv import load_dotenv
from tool_registry import TOOL_DEFINITIONS, TOOL_HANDLERS
from openai import AzureOpenAI
from azure.search.documents import SearchClient
from azure.search.documents.models import VectorizedQuery
from azure.core.credentials import AzureKeyCredential

load_dotenv()

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
    Ask Claude to classify the user's intent into one of three categories:
      - 'greeting'       : casual message with no policy question
      - 'list_policies'  : user wants to see all/available policies
      - 'rag'            : specific policy question that needs RAG

    Falls back to 'rag' on any error so the pipeline always continues.
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
                    "- \"greeting\" — casual greeting or small talk with no policy question "
                    "(e.g. hi, hello, good morning, thanks, bye)\n"
                    "- \"list_policies\" — user wants to see all or available company policies "
                    "(e.g. list all policies, what policies do you have, show me all docs)\n"
                    "- \"rag\" — any specific question about a policy, leave, benefit, or HR topic\n\n"
                    "IMPORTANT: If the message contains BOTH a greeting AND a policy question "
                    "(e.g. 'good morning, what is the leave policy?'), classify as \"rag\".\n\n"
                    f"Message: {question}\n\n"
                    "Reply with only the single category word."
                )
            }],
        )
        intent = next((b.text for b in response.content if b.type == "text"), "").strip().lower()
        return intent if intent in ("greeting", "list_policies", "rag") else "rag"
    except Exception as e:
        print(f"[intent] classifier failed, defaulting to rag: {e}")
        return "rag"


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
# MISTRAL FUNCTION CALLING ROUTER  (new)
#
# Mistral reads the user's question + tool descriptions and decides:
#   - Which Zoho tool to call (get_leave_balance / apply_leave)
#   - OR return nothing → fall through to RAG
#
# To add a new tool: only edit tool_registry.py. This function never changes.
# =========================
def ask_policy_question(question: str, employee_email: str = ""):
    # Classify intent — handles any phrasing, no keyword lists needed
    intent = _classify_intent(question)

    # 1. Greeting — no RAG, no links, no disclaimer
    if intent == "greeting":
        return "Good day! 👋 How can I help you with Caizin's policies today?"

    # 2. List all policies — bypass RAG, fetch directly from index
    if intent == "list_policies":
        return list_all_policies()

    # 3. Tool-use pass — let Claude decide if this needs a live Keka action
    messages = [{"role": "user", "content": question}]
    response = _get_anthropic_client().messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        tools=TOOL_DEFINITIONS,
        messages=messages,
    )

    if response.stop_reason == "tool_use":
        while response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    handler = TOOL_HANDLERS.get(block.name)
                    result = handler(block.input, employee_email) if handler else f"Unknown tool: {block.name}"
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})
            response = _get_anthropic_client().messages.create(
                model=CLAUDE_MODEL,
                max_tokens=1024,
                tools=TOOL_DEFINITIONS,
                messages=messages,
            )
        return next((b.text for b in response.content if b.type == "text"), "")

    # 4. RAG pipeline — no tool matched, answer from policy documents
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
            f"📎 View Full Policy:\n"
           f"- {policy_name}: {policy_url}\n"
        )

    if is_policy_answer:
        answer += (
            "\n---\n"
            "_For final confirmation and official applicability, "
            "please verify the policy details with HR._"
        )

    # Add styled disclaimer (Option 2 formatting)
    answer += (
        "\n---\n"
        "_For final confirmation and official applicability, "
        "please verify the policy details with HR._"
    )

    return answer


if __name__ == "__main__":
    question = input("Ask a policy question: ")
    print("\nAnswer:\n")
    print(ask_policy_question(question))
