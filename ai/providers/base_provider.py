from abc import ABC, abstractmethod
from collections.abc import Iterator


class BaseAIProvider(ABC):
    """
    Common interface implemented by every AI provider.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        pass

    @property
    @abstractmethod
    def model(self) -> str:
        pass

    @abstractmethod
    def generate(self, prompt: str) -> str:
        pass

    @abstractmethod
    def stream(self, prompt: str) -> Iterator[str]:
        pass
