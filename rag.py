import os
import json
import requests
from dotenv import load_dotenv
from openai import AzureOpenAI
from azure.search.documents import SearchClient
from azure.search.documents.models import VectorizedQuery
from azure.core.credentials import AzureKeyCredential

load_dotenv()

# =========================
# CONFIG  (unchanged)
# =========================
SEARCH_ENDPOINT = os.getenv("AZURE_SEARCH_ENDPOINT")
SEARCH_KEY = os.getenv("AZURE_SEARCH_KEY")
INDEX_NAME = os.getenv("AZURE_SEARCH_INDEX")

AZURE_OPENAI_API_KEY = os.getenv("AZURE_OPENAI_API_KEY")
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT")

MISTRAL_KEY = os.getenv("MISTRAL_API_KEY")

# =========================
# CLIENTS  (lazy — created on first use so module import never blocks)
# =========================
_search_client = None
_azure_client  = None

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
        k_nearest_neighbors=8,
        fields="embedding"
    )

    results = _get_search_client().search(
        search_text=query,
        vector_queries=[vector_query],
        top=8
    )

    docs = []
    seen = set()
    sources = {}

    for r in results:
        content = r.get("content")
        policy_name = r.get("policy_name")
        policy_url = r.get("policy_url")

        if content and content not in seen:
            docs.append(content)
            seen.add(content)

        if policy_name and policy_url:
            sources[policy_name] = policy_url

    return docs[:12], sources

# =========================
# MISTRAL GENERATION  (unchanged)
# =========================
def generate_answer(question: str, context_docs: list):
    if not context_docs:
        return "I couldn't find this in the company policy."

    context = "\n\n".join(context_docs)

    prompt = f"""
You are an internal Caizin company policy assistant.

    CRITICAL RULES:
    - Answer ONLY using the provided context.
    - Do NOT assume eligibility.
    - When a policy defines an explicit list (e.g. spouse, parent, child, sibling):
    - Treat the list as CLOSED.
    - If the user is NOT eligible for a specific leave:
    - Clearly state the ineligibility and the reason.
    - HANDLING INELIGIBILITY:
       - If the user is NOT eligible, clearly state the reason based on the text.
       - IF AND ONLY IF the user asked about a specific "Leave Type" (like Sick Leave), you may list other available leave types.
       - IF the user asked about "Benefits" or "Reimbursements" (like Gym/Fitness), DO NOT list leave types. Only mention alternative financial benefits if they exist in the text.
    - Do NOT invent leave categories.
    - For numeric values, copy them EXACTLY as written.
    - If no alternatives are mentioned in the policy, state that explicitly.

    Context:
    {context}

    Question:
    {question}

"""
    response = requests.post(
        "https://api.mistral.ai/v1/chat/completions",
        headers={"Authorization": f"Bearer {MISTRAL_KEY}"},
        json={
            "model": "mistral-small-latest",
            "messages": [
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.0
        }
    )

    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"]


# =========================
# MISTRAL FUNCTION CALLING ROUTER  (new)
#
# Mistral reads the user's question + tool descriptions and decides:
#   - Which Zoho tool to call (get_leave_balance / apply_leave)
#   - OR return nothing → fall through to RAG
#
# To add a new tool: only edit tool_registry.py. This function never changes.
# =========================
from tool_registry import TOOL_DEFINITIONS, TOOL_HANDLERS


def _route_to_tool(question: str, employee_email: str):
    """
    Ask Mistral to pick a tool via function calling.
    Returns the tool's response string, or None to fall through to RAG.
    """
    try:
        resp = requests.post(
            "https://api.mistral.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {MISTRAL_KEY}"},
            json={
                "model": "mistral-small-latest",
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are an HR assistant. "
                            "If the user's question requires live data from Zoho People "
                            "(like their leave balance or applying for leave), "
                            "call the appropriate tool. "
                            "If the question is about company policy, rules, or entitlements, "
                            "do NOT call any tool — return no tool call so the RAG pipeline handles it. "
                            f"The employee's email is: {employee_email}"
                        ),
                    },
                    {"role": "user", "content": question},
                ],
                "tools":       TOOL_DEFINITIONS,
                "tool_choice": "auto",
                "temperature": 0.0,
            },
            timeout=15,
        )
        resp.raise_for_status()

    except Exception as e:
        # If routing call fails, fall through to RAG silently
        print(f"[router] Mistral function calling failed: {e}")
        return None

    message = resp.json()["choices"][0]["message"]

    # No tool selected → fall through to RAG
    if not message.get("tool_calls"):
        return None

    tool_call = message["tool_calls"][0]
    tool_name = tool_call["function"]["name"]
    raw_args  = tool_call["function"].get("arguments", "{}")
    tool_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args

    handler = TOOL_HANDLERS.get(tool_name)
    if not handler:
        print(f"[router] Unknown tool selected by Mistral: {tool_name}")
        return None

    return handler(tool_args, employee_email)


# =========================
# MAIN ENTRY  (updated signature)
# =========================
def ask_policy_question(question: str, employee_email: str = ""):
    """
    Called by teams_bot.py.

    Flow:
      1. If we have the employee's email, ask Mistral which tool to call
      2. Tool selected → call Zoho, return result immediately
      3. No tool / no email → Azure AI Search + Mistral generate (RAG — unchanged)
    """

    # Step 1 & 2: Try Zoho routing
    if employee_email:
        tool_result = _route_to_tool(question, employee_email)
        if tool_result:
            return tool_result

    # Step 3: RAG fallback — your original pipeline, completely unchanged
    docs, sources = search_documents(question)
    answer = generate_answer(question,docs)
    if sources:
        first_policy = next(iter(sources.items()))
        policy_name, policy_url = first_policy

        answer += (
            "\n\n---\n"
            f"📎 View Full Policy:\n"
            f"- {policy_name}: {policy_url}\n"
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
    answer = ask_policy_question(question)
    print("\nAnswer:\n")
    print(answer)