from decimal import Decimal
from pathlib import Path

from langchain_core.documents import Document
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_postgres import PGVector
from langchain_text_splitters import RecursiveCharacterTextSplitter
from pydantic import BaseModel, ConfigDict

from agent.settings import Settings

COLLECTION = "contracts"


class Tolerance(BaseModel):
    # Decimal is not usable with strict structured outputs, so this call uses JSON mode
    # and the prompt describes the fields with an example (ADR-008). No extra keys: an
    # answer wrapped in {"properties": ...} is rejected, never unwrapped.
    model_config = ConfigDict(extra="forbid")
    tolerance_pct: Decimal | None
    evidence: str


def make_store(settings: Settings) -> PGVector:
    embeddings = OpenAIEmbeddings(
        model="o2c-embed",
        base_url=settings.litellm_base_url,
        api_key=settings.litellm_master_key,
        check_embedding_ctx_length=False,
    )
    return PGVector(
        embeddings=embeddings,
        collection_name=COLLECTION,
        connection=settings.events_database_url,
        use_jsonb=True,
    )


def ingest_contracts(store: PGVector, contracts_dir: Path) -> int:
    """Rebuild the contract index from scratch, so running it twice never duplicates chunks."""
    splitter = RecursiveCharacterTextSplitter(chunk_size=800, chunk_overlap=100)
    docs = []
    for path in sorted(contracts_dir.glob("*.md")):
        customer_number = path.stem.split("-")[0]
        for chunk in splitter.split_text(path.read_text(encoding="utf-8")):
            docs.append(
                Document(
                    page_content=chunk,
                    metadata={"customer_number": customer_number, "source": path.name},
                )
            )
    store.delete_collection()
    store.create_collection()
    store.add_documents(docs)
    return len(docs)


class ToleranceLookup:
    """Reads a customer's price tolerance from their contract (RAG). One LLM call per customer."""

    def __init__(self, store: PGVector, chain, prompts_dir: Path):
        self._store, self._chain = store, chain
        self._prompt = (prompts_dir / "tolerance.md").read_text(encoding="utf-8")
        self._cache: dict[str, Tolerance] = {}

    def __call__(self, customer_number: str) -> Tolerance:
        if customer_number in self._cache:
            return self._cache[customer_number]
        hits = self._store.similarity_search(
            "price tolerance deviation percent",
            k=2,
            filter={"customer_number": customer_number},
        )
        if not hits:
            return Tolerance(tolerance_pct=None, evidence="no contract found")
        context = "\n---\n".join(h.page_content for h in hits)
        result = self._chain.invoke([("system", self._prompt), ("human", context)])
        self._cache[customer_number] = result
        return result


def structured_tolerance(llm: ChatOpenAI):
    """The LLM in JSON mode, parsed into a Tolerance (OutputParserException otherwise)."""
    return llm.with_structured_output(Tolerance, method="json_mode")


def make_tolerance_lookup(settings: Settings, store: PGVector) -> ToleranceLookup:
    llm = ChatOpenAI(
        model="o2c-extract",
        base_url=settings.litellm_base_url,
        api_key=settings.litellm_master_key,
        temperature=0,
        extra_body={"metadata": {"trace_name": "tolerance", "tags": ["agent"]}},
    )
    return ToleranceLookup(
        store,
        structured_tolerance(llm),
        settings.prompts_dir,
    )


if __name__ == "__main__":
    s = Settings()
    print(f"ingested {ingest_contracts(make_store(s), s.contracts_dir)} chunks")
