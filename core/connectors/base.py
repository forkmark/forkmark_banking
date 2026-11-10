"""Connector protocols and abstract interfaces for governed model endpoints."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


@runtime_checkable
class Connector(Protocol):
    """Protocol for model endpoint adapters."""

    def chat(
        self,
        messages: List[Dict[str, Any]],
        *,
        model: Optional[str] = None,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        timeout: float = 60.0,
        transport: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Synchronously execute a chat completion request."""
        ...

    async def achat(
        self,
        messages: List[Dict[str, Any]],
        *,
        model: Optional[str] = None,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        timeout: float = 60.0,
        transport: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Asynchronously execute a chat completion request."""
        ...

    def embed(
        self,
        texts: List[str],
        *,
        model: Optional[str] = None,
        timeout: float = 30.0,
        transport: Optional[Any] = None,
    ) -> List[List[float]]:
        """Synchronously generate text embeddings."""
        ...

    def health(
        self,
        *,
        timeout: float = 15.0,
        transport: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Perform a connectivity and health probe."""
        ...

