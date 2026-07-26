from abc import ABC, abstractmethod


class BaseDriver(ABC):

    @abstractmethod
    def connect(self):
        pass

    @abstractmethod
    def disconnect(self):
        pass

    @abstractmethod
    def read(self, tag):
        pass

    @abstractmethod
    def read_all(self, tags):
        pass

    @abstractmethod
    def write(self, tag, value):
        pass
