import os
import uuid
import time
from urllib.parse import quote
from dotenv import load_dotenv
from pypdf import PdfReader
from openai import AzureOpenAI
from azure.search.documents import SearchClient
from azure.core.credentials import AzureKeyCredential

load_dotenv()

# ==========================
# ENV CONFIG
# ==========================
SEARCH_ENDPOINT = os.getenv("AZURE_SEARCH_ENDPOINT")
SEARCH_KEY = os.getenv("AZURE_SEARCH_KEY")
INDEX_NAME = os.getenv("AZURE_SEARCH_INDEX")

AZURE_OPENAI_API_KEY = os.getenv("AZURE_OPENAI_API_KEY")
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT")

DATA_FOLDER = "data"

# ==========================
# CLIENTS
# ==========================
search_client = SearchClient(
    endpoint=SEARCH_ENDPOINT,
    index_name=INDEX_NAME,
    credential=AzureKeyCredential(SEARCH_KEY),
)

azure_client = AzureOpenAI(
    api_key=AZURE_OPENAI_API_KEY,
    azure_endpoint=AZURE_OPENAI_ENDPOINT,
    api_version="2024-02-15-preview"
)

# ==========================
# EMBEDDINGS
# ==========================
def get_embeddings_batch(text_list):
    if not isinstance(text_list, list):
        text_list = [text_list]

    response = azure_client.embeddings.create(
        model=AZURE_OPENAI_DEPLOYMENT,
        input=text_list
    )

    return [item.embedding for item in response.data]


# ==========================
# FILE READERS
# ==========================
def read_pdf(path):
    reader = PdfReader(path)
    text = ""
    for page in reader.pages:
        text += page.extract_text() or ""
    return text


def read_txt(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# ==========================
# CHUNKING
# ==========================
def chunk_text(text, chunk_size=1500, overlap=300):
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end])
        start += chunk_size - overlap
    return chunks


# ==========================
# CLEAR INDEX (SAFE FULL WIPE)
# ==========================
def clear_index():
    print("🧹 Clearing existing index...")

    while True:
        results = search_client.search(search_text="*", top=1000)
        ids = [{"id": doc["id"]} for doc in results]

        if not ids:
            break

        search_client.delete_documents(ids)
        print(f"🗑 Deleted {len(ids)} documents")
        time.sleep(0.5)

    print("✅ Index cleared successfully.\n")


# ==========================
# INGEST FILE
# ==========================
def ingest_file(file_path):
    print(f"📄 Processing: {file_path}")

    if file_path.endswith(".pdf"):
        text = read_pdf(file_path)
    elif file_path.endswith(".txt"):
        text = read_txt(file_path)
    else:
        return

    chunks = chunk_text(text)
    print(f"🔹 Total chunks: {len(chunks)}")

    filename = os.path.basename(file_path)
    policy_name = os.path.splitext(filename)[0].strip()

    STORAGE_ACCOUNT = os.getenv("AZURE_STORAGE_ACCOUNT")
    BLOB_CONTAINER = "policy-pdfs"

    encoded_filename = quote(filename)
    policy_url = f"https://{STORAGE_ACCOUNT}.blob.core.windows.net/{BLOB_CONTAINER}/{encoded_filename}"

    documents = []
    EMBED_BATCH_SIZE = 5

    for i in range(0, len(chunks), EMBED_BATCH_SIZE):
        batch_chunks = chunks[i:i + EMBED_BATCH_SIZE]
        embeddings = get_embeddings_batch(batch_chunks)

        for chunk, embedding in zip(batch_chunks, embeddings):
            documents.append({
                "id": str(uuid.uuid4()),
                "content": chunk,
                "department": filename,
                "policy_name": policy_name,
                "policy_url": policy_url,
                "embedding": embedding
            })

        time.sleep(1)

    AZURE_BATCH_SIZE = 10

    for i in range(0, len(documents), AZURE_BATCH_SIZE):
        batch = documents[i:i + AZURE_BATCH_SIZE]
        search_client.upload_documents(batch)
        print(f"⬆ Uploaded Azure batch {i//AZURE_BATCH_SIZE + 1}")
        time.sleep(0.5)

    print(f"✅ Uploaded {len(documents)} chunks total\n")


# ==========================
# MAIN
# ==========================
def ingest_all():
    clear_index()

    for root, _, files in os.walk(DATA_FOLDER):
        for file in files:
            if file.endswith(".pdf") or file.endswith(".txt"):
                ingest_file(os.path.join(root, file))

    print("\n🎉 All documents ingested successfully!")


if __name__ == "__main__":
    ingest_all()
