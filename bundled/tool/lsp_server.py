# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.
"""Implementation of tool support over LSP."""
from __future__ import annotations
import asyncio

# import websockets

import copy
import json
import os
import pathlib
from time import sleep
import re
import sys
import sysconfig
import traceback
from typing import Any, Optional, Dict, Sequence


# **********************************************************
# Update sys.path before importing any bundled libraries.
# **********************************************************
def update_sys_path(path_to_add: str, strategy: str) -> None:
    """Add given path to `sys.path`."""
    if path_to_add not in sys.path and os.path.isdir(path_to_add):
        if strategy == "useBundled":
            sys.path.insert(0, path_to_add)
        elif strategy == "fromEnvironment":
            sys.path.append(path_to_add)


# Ensure that we can import LSP libraries, and other bundled libraries.
update_sys_path(
    os.fspath(pathlib.Path(__file__).parent.parent / "libs"),
    os.getenv("LS_IMPORT_STRATEGY", "useBundled"),
)

# **********************************************************
# Imports needed for the language server goes below this.
# **********************************************************
# pylint: disable=wrong-import-position,import-error
import lsp_jsonrpc as jsonrpc
import lsp_utils as utils
import lsprotocol.types as lsp
from pygls import server, uris, workspace

WORKSPACE_SETTINGS = {}
GLOBAL_SETTINGS = {}
RUNNER = pathlib.Path(__file__).parent / "lsp_runner.py"

MAX_WORKERS = 5
LSP_SERVER = server.LanguageServer(
    name="VoiceControl", version="0.1.0", max_workers=MAX_WORKERS
)

# **********************************************************
# Speech to text and text to command
# **********************************************************
from transformers import pipeline, WhisperTokenizer, WhisperFeatureExtractor
from transformers.pipelines.audio_utils import ffmpeg_microphone_live
import torch
import io
import threading
from contextlib import redirect_stdout

import text2command
import commands
import platform

# Uncomment this line to see all of the possible wake words
# print(classifier.model.config.id2label)


# Once "Using microphone:" is printed that's when mic starts listening #TEST
def monitor_microphone_output(output_buffer, stop_event):
    system = platform.system()
    if system != "Windows":
        LSP_SERVER.send_notification("custom/notification", {"content": "listen"})
        stop_event.set()

    while not stop_event.is_set():
        if "Using microphone:" in output_buffer.getvalue():
            LSP_SERVER.send_notification("custom/notification", {"content": "listen"})
            stop_event.set()  # Stop monitoring once the message is detected


# Transcribes speech and converts it to text
def transcribe(chunk_length_s=5.0, stream_chunk_s=0.75):
    sampling_rate = transcriber.feature_extractor.sampling_rate
    # Set up a buffer to capture stdout
    output_buffer = io.StringIO()
    stop_event = threading.Event()

    # Start monitoring output in a separate thread
    monitor_thread = threading.Thread(
        target=monitor_microphone_output, args=(output_buffer, stop_event)
    )
    monitor_thread.start()
    with redirect_stdout(output_buffer):
        mic = ffmpeg_microphone_live(
            sampling_rate=sampling_rate,
            chunk_length_s=chunk_length_s,
            stream_chunk_s=stream_chunk_s,
        )

        num_inferences = 1
        phrase = ""

        for item in transcriber(
            mic,
            generate_kwargs={
                "max_new_tokens": 128,
                "forced_decoder_ids": forced_decoder_ids,
            },
        ):
            # Uncomment to see the prediction as it happens
            # sys.stdout.write("\033[K")
            log_to_output(str(item))
            # print(item["text"], end="\r")
            if num_inferences > 2 and item["text"][-1] == ".":
                break
            num_inferences += 1
            if phrase == item["text"]:
                break
            phrase = item["text"]
            # This if statement should never be hit for commands longer than a word
            if not item["partial"][0]:
                break
    # Ensure the monitoring thread stops
    stop_event.set()
    monitor_thread.join()

    log_to_output("Finished transcribing")
    return item["text"]


# Listens for wake word (go) and calls transcribe
def listen_for_wake_word(
    wake_word="go",
    prob_threshold=0.5,
    chunk_length_s=0.5,
    stream_chunk_s=0.25,
    debug=False,
):

    sampling_rate = classifier.feature_extractor.sampling_rate

    mic = ffmpeg_microphone_live(
        sampling_rate=sampling_rate,
        chunk_length_s=chunk_length_s,
        stream_chunk_s=stream_chunk_s,
    )

    LSP_SERVER.send_notification("custom/notification", {"content": "wake"})
    log_to_output("Listening for wake word...")
    while True:
        for prediction in classifier(mic):
            # Uncomment these lines to see the wake word prediction with score
            # log_to_output(prediction[0]["label"])
            # log_to_output(str(prediction[0]["score"]))
            prediction = prediction[0]
            if prediction["label"] == wake_word or prediction["label"] == "no":
                if prediction["score"] > prob_threshold:
                    LSP_SERVER.send_notification(
                        "custom/notification", {"content": "loading"}
                    )
                    log_to_output("Please say a command")
                    result = transcribe(chunk_length_s=20.0)
                    log_to_output("You said: " + result)
                    command = text2command.findSimilarPhrases(
                        result,
                        locale,
                        enableCommandSuggestions,
                        numberCommandSuggestions,
                    )
                    log_to_output(command[0])
                    if (
                        command[0] == "Command not found"
                        or command[0] == "Command not renamed"
                    ):
                        LSP_SERVER.send_notification(
                            "custom/notification",
                            {"content": command[0], "parameters": command[1]},
                        )
                    elif (
                        command[0] == "Renaming Command: Final"
                        or command[0] == "Display command suggestions"
                        or command[0] == "Command Group"
                    ):
                        LSP_SERVER.send_notification(
                            "custom/notification",
                            {"content": command[0], "parameters": command[1:]},
                        )
                    else:
                        LSP_SERVER.send_notification(
                            "custom/notification", {"content": command[0]}
                        )
                    prediction["label"] = ""
            sleep(0.250)  # Decreases load on cpu


# **********************************************************
# Required Language Server Initialization and Exit handlers.
# **********************************************************
@LSP_SERVER.feature(lsp.INITIALIZE)
def initialize(params: lsp.InitializeParams) -> None:
    """LSP handler for initialize request."""
    log_to_output(f"CWD Server: {os.getcwd()}")

    paths = "\r\n   ".join(sys.path)
    log_to_output(f"sys.path used to run Server:\r\n   {paths}")

    GLOBAL_SETTINGS.update(**params.initialization_options.get("globalSettings", {}))

    settings = params.initialization_options["settings"]
    log_to_output(f"The current locale for VS Code is: {params.locale}")
    _update_workspace_settings(settings)
    log_to_output(
        f"Settings used to run Server:\r\n{json.dumps(settings, indent=4, ensure_ascii=False)}\r\n"
    )
    log_to_output(
        f"Global settings:\r\n{json.dumps(GLOBAL_SETTINGS, indent=4, ensure_ascii=False)}\r\n"
    )

    global locale
    locale = params.locale
    log_to_output(f"Using the language {commands.convert_locale_language[locale]}")

    global enableCommandSuggestions
    enableCommandSuggestions = params.initialization_options["enableCommandSuggestions"]
    log_to_output(f"Enable command suggestions is {enableCommandSuggestions}")

    global numberCommandSuggestions
    numberCommandSuggestions = params.initialization_options["numberCommandSuggestions"]
    log_to_output(f"Number of command suggestions is {numberCommandSuggestions}")

    device = "cuda:0" if torch.cuda.is_available() else "cpu"
    global classifier
    classifier = pipeline(
        "audio-classification",
        model="MIT/ast-finetuned-speech-commands-v2",
        device=device,
    )
    global tokenizer
    tokenizer = WhisperTokenizer.from_pretrained(
        "openai/whisper-base",
        language=commands.convert_locale_language[locale],
        task="transcribe",
    )
    global transcriber
    transcriber = pipeline(
        "automatic-speech-recognition",
        model="openai/whisper-base",
        device=device,
        tokenizer=tokenizer,
    )
    global forced_decoder_ids
    forced_decoder_ids = tokenizer.get_decoder_prompt_ids(
        language=commands.convert_locale_language[locale], task="transcribe"
    )


@LSP_SERVER.feature(lsp.INITIALIZED)
def initialized(params: lsp.InitializedParams) -> None:
    """Handler for initialized"""
    listen_for_wake_word()
    log_error("We should never get here")


# **********************************************************
# Sending/Receiving Messages from the Server
# **********************************************************
@LSP_SERVER.feature(lsp.EXIT)
def on_exit(_params: Optional[Any] = None) -> None:
    """Handle clean up on exit."""
    jsonrpc.shutdown_json_rpc()


@LSP_SERVER.feature(lsp.SHUTDOWN)
def on_shutdown(_params: Optional[Any] = None) -> None:
    """Handle clean up on shutdown."""
    jsonrpc.shutdown_json_rpc()


def _get_global_defaults():
    return {
        "path": GLOBAL_SETTINGS.get("path", []),
        "interpreter": GLOBAL_SETTINGS.get("interpreter", [sys.executable]),
        "args": GLOBAL_SETTINGS.get("args", []),
        "importStrategy": GLOBAL_SETTINGS.get("importStrategy", "useBundled"),
        "showNotifications": GLOBAL_SETTINGS.get("showNotifications", "off"),
    }


def _update_workspace_settings(settings):
    if not settings:
        key = os.getcwd()
        WORKSPACE_SETTINGS[key] = {
            "cwd": key,
            "workspaceFS": key,
            "workspace": uris.from_fs_path(key),
            **_get_global_defaults(),
        }
        return

    for setting in settings:
        key = uris.to_fs_path(setting["workspace"])
        WORKSPACE_SETTINGS[key] = {
            "cwd": key,
            **setting,
            "workspaceFS": key,
        }


# *****************************************************
# Logging and notification.
# *****************************************************
def log_to_output(
    message: str, msg_type: lsp.MessageType = lsp.MessageType.Log
) -> None:
    LSP_SERVER.show_message_log(message, msg_type)


def log_error(message: str) -> None:
    LSP_SERVER.show_message_log(message, lsp.MessageType.Error)
    if os.getenv("LS_SHOW_NOTIFICATION", "off") in ["onError", "onWarning", "always"]:
        LSP_SERVER.show_message(message, lsp.MessageType.Error)


def log_warning(message: str) -> None:
    LSP_SERVER.show_message_log(message, lsp.MessageType.Warning)
    if os.getenv("LS_SHOW_NOTIFICATION", "off") in ["onWarning", "always"]:
        LSP_SERVER.show_message(message, lsp.MessageType.Warning)


def log_always(message: str) -> None:
    LSP_SERVER.show_message_log(message, lsp.MessageType.Info)
    if os.getenv("LS_SHOW_NOTIFICATION", "off") in ["always"]:
        LSP_SERVER.show_message(message, lsp.MessageType.Info)


# *****************************************************
# Start the server.
# *****************************************************
if __name__ == "__main__":
    LSP_SERVER.start_io()
