import logging
import os
from datetime import datetime
from logging.config import DictConfigurator


class BotLoggerFileHandler(logging.FileHandler):
    def __init__(self, *args, **kwargs):
        _folder = ".logs/"
        os.makedirs(_folder, exist_ok=True)
        self.basefilename = f"{_folder}bot-{int(datetime.now().timestamp())}.log"
        # self.bot_name = None
        super().__init__(self.basefilename)

    # def should_setup_filename(self, record):
    #     if self.bot_name is None:
    #         # tenta extrair o nome do bot do logger
    #         logger_name = record.botname if hasattr(record, "botname") else None
    #         if logger_name:
    #             self.bot_name = logger_name.split(".")[0]
    #             return True
    #     return False

    # def setup_filename(self):
    #     self.basefilename = f".logs/[bot]-{int(datetime.now().timestamp())}.log"
    #     """
    #     Do a rollover, as described in __init__().
    #     """
    #     if self.stream:
    #         self.stream.close()
    #         self.stream = None # type: ignore
    #     if self.bot_name:
    #         dfn = (self.basefilename % self.bot_name)
    #         if os.path.exists(dfn):
    #             os.remove(dfn)
    #         else:
    #             if os.path.exists(self.basefilename):
    #                 os.rename(self.basefilename, dfn)
    #     if not self.delay:
    #         self.stream = self._open()

    # def emit(self, record):
    #     """
    #     Emit a record.

    #     Output the record to the file, catering for rollover as described
    #     in doRollover().
    #     """
    #     try:
    #         print("AAAAAAAAAAAAAAAAAAAAAAAA")
    #         print(record)
    #         if self.should_setup_filename(record):
    #             self.setup_filename()
    #         logging.FileHandler.emit(self, record)
    #     except Exception:
    #         self.handleError(record)


LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "format": "%(asctime)s %(levelname)s %(name)s.%(funcName)s %(message)s",
        },
        "console": {
            "format": "%(name)s.%(funcName)s - %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "rich.logging.RichHandler",
            "formatter": "console",
            "level": "INFO",
        },
        "file": {
            "class": "logging_config.BotLoggerFileHandler",
            "formatter": "default",
            "level": "DEBUG",
        },
    },
    "root": {
        "handlers": ["console", "file"],
        "level": "DEBUG",
    },
    "loggers": {
        "httpcore": {
            "level": "ERROR",
        },
    },
}


def setup_logging():
    DictConfigurator(LOGGING).configure()
