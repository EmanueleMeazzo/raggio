import asyncio

import httpx


class Embedder:
    """Client for OpenAI-compatible /embeddings endpoints (OpenAI, Azure Foundry, ...)."""

    def __init__(self, base_url: str, api_key: str, model: str) -> None:
        if not base_url:
            raise ValueError("no embedding endpoint configured (EMBEDDING_BASE_URL)")
        base = base_url.rstrip("/")
        # Accept both ".../v1" bases and full Azure-style URLs that already contain /embeddings
        self.url = base if "/embeddings" in base else base + "/embeddings"
        self.model = model
        headers = {}
        if api_key:
            # both header styles so OpenAI and Azure endpoints work unchanged
            headers = {"Authorization": f"Bearer {api_key}", "api-key": api_key}
        self._client = httpx.AsyncClient(headers=headers, timeout=60.0)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        payload: dict = {"input": texts}
        if self.model:
            payload["model"] = self.model
        last: Exception | None = None
        for attempt in range(3):
            try:
                r = await self._client.post(self.url, json=payload)
                r.raise_for_status()
                data = r.json()["data"]
                data.sort(key=lambda d: d["index"])
                return [d["embedding"] for d in data]
            except Exception as e:  # ponytail: retry everything 3x; per-status handling if a provider needs it
                last = e
                await asyncio.sleep(2**attempt)
        raise RuntimeError(f"embedding request to {self.url} failed after 3 attempts: {last}")

    async def aclose(self) -> None:
        await self._client.aclose()
