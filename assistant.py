# ╔══════════════════════════════════════════════════════════════╗
# ║          AIVA - AI Voice Assistant  v3.0  (Fixed)           ║
# ║  Wake Word: "AIVA"  |  Stop: "stop" / "goodbye"            ║
# ║  http://localhost:5000                                       ║
# ╚══════════════════════════════════════════════════════════════╝

from __future__ import with_statement
import pyttsx3
import speech_recognition as sr
import datetime
import wikipedia
import webbrowser
import random
import pyautogui
import time
import requests
import google.generativeai as genai
import os
import re
import operator
import socket
import threading
import subprocess
import sys

from flask import Flask, jsonify, Response, render_template, request
from flask_cors import CORS
import psutil
import cv2

from rapidfuzz import fuzz, process

# ──────────────────────────────────────────────────────────────
# Optional noise-cancellation via noisereduce
# Install:  pip install noisereduce soundfile numpy
# If not installed, we fall back gracefully.
# ──────────────────────────────────────────────────────────────
try:
    import numpy as np
    import noisereduce as nr
    import soundfile as sf
    import io
    NOISE_REDUCE_AVAILABLE = True
    print("Noise reduction library loaded.")
except ImportError:
    NOISE_REDUCE_AVAILABLE = False
    print("⚠️  noisereduce not installed — running without noise cancellation.")
    print("    To enable: pip install noisereduce soundfile numpy")

# ──────────────────────────────────────────────────────────────
# Flask App
# ──────────────────────────────────────────────────────────────
app = Flask(__name__, static_url_path='/static', static_folder='static')
CORS(app)

# Disable Flask request logging
import logging
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)
app.logger.disabled = True

print("AIVA Assistant starting...")

# ──────────────────────────────────────────────────────────────
# ACTIVITY LOG — for real-time display on web UI
# ──────────────────────────────────────────────────────────────
from collections import deque
activity_log = deque(maxlen=20)  # Keep last 20 activities
activity_lock = threading.Lock()

def log_activity(status, message):
    """Log an activity with timestamp."""
    import datetime
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    with activity_lock:
        activity_log.append({
            "timestamp": timestamp,
            "status": status,
            "message": message
        })
    # Also print to console
    print(f"[{timestamp}] {status}: {message}")

# ──────────────────────────────────────────────────────────────
# STATE — always-on listening with auto-execute after silence
# ──────────────────────────────────────────────────────────────
STOP_WORDS      = {"stop", "goodbye", "bye", "sleep", "pause", "exit"}
SILENCE_TIMEOUT = 2.5              # Seconds of silence to auto-execute command
assistant_active = True            # Always active (always listening)
assistant_lock   = threading.Lock()
command_queue    = []              # Queue for pending commands
last_command_time = time.time()    # Track when last command was spoken

# ──────────────────────────────────────────────────────────────
# TTS — fresh engine per call (fixes "run loop already started")
# ──────────────────────────────────────────────────────────────
_tts_lock = threading.Lock()

def speak(text):
    """Thread-safe TTS. Creates a fresh engine every call."""
    if not text or str(text).strip() == "":
        return
    with _tts_lock:
        try:
            eng = pyttsx3.init('sapi5')
            voices = eng.getProperty('voices')
            if len(voices) > 1:
                eng.setProperty('voice', voices[1].id)   # Female voice
            eng.setProperty('rate', 165)
            eng.setProperty('volume', 1.0)
            eng.say(str(text))
            eng.runAndWait()
            eng.stop()
        except Exception as e:
            print(f"[TTS error] {e}")

# ──────────────────────────────────────────────────────────────
# RECOGNIZER — global, tuned once
# ──────────────────────────────────────────────────────────────
recognizer = sr.Recognizer()
recognizer.energy_threshold        = 2000   # lower = more sensitive
recognizer.dynamic_energy_threshold = True
recognizer.pause_threshold          = 0.7   # shorter pause = faster response
recognizer.non_speaking_duration    = 0.4

def _apply_noise_reduction(audio_data: sr.AudioData) -> sr.AudioData:
    """
    Apply noisereduce to an AudioData object.
    Returns original if library unavailable or if reduction fails.
    """
    if not NOISE_REDUCE_AVAILABLE:
        return audio_data
    try:
        raw   = audio_data.get_raw_data(convert_rate=16000, convert_width=2)
        arr   = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
        clean = nr.reduce_noise(y=arr, sr=16000, stationary=False, prop_decrease=0.75)
        clean_int = clean.astype(np.int16)
        return sr.AudioData(clean_int.tobytes(), 16000, 2)
    except Exception as e:
        print(f"[Noise reduction skipped] {e}")
        return audio_data


def listen_once(timeout=6, phrase_limit=7, calibrate=False) -> str:
    """
    Record one utterance and return recognised text (lowercase).
    Returns "" on failure.
    """
    try:
        with sr.Microphone() as source:
            if calibrate:
                recognizer.adjust_for_ambient_noise(source, duration=1.0)
            else:
                recognizer.adjust_for_ambient_noise(source, duration=0.3)
            try:
                audio = recognizer.listen(source, timeout=timeout, phrase_time_limit=phrase_limit)
            except sr.WaitTimeoutError:
                return ""

        audio = _apply_noise_reduction(audio)

        text = recognizer.recognize_google(audio, language='en-in')
        print(f"  🎙  Heard: {text}")
        return text.lower().strip()

    except sr.UnknownValueError:
        return ""
    except sr.RequestError as e:
        print(f"[Google STT network error] {e}")
        return ""
    except Exception as e:
        print(f"[Microphone error] {e}")
        time.sleep(0.5)
        return ""

# ──────────────────────────────────────────────────────────────
# ALWAYS-ON LISTENING LOOP — auto-execute after silence
# ──────────────────────────────────────────────────────────────
def always_listening_loop():
    """
    Continuously listen for speech.
    When silence detected for 2.5 seconds, auto-execute the command.
    Then return to listening immediately.
    """
    global assistant_active, last_command_time
    
    # One-time calibration at startup
    print("📡 Calibrating microphone (2s)… please be quiet.")
    try:
        with sr.Microphone() as source:
            recognizer.adjust_for_ambient_noise(source, duration=2)
    except Exception as e:
        print(f"Calibration error: {e}")

    wish_me()
    print(f'\n🟢 AIVA is ready. Speak any command and I will execute it.\n')
    
    accumulated_text = ""  # Build up speech fragments
    silence_start_time = None
    
    while True:
        try:
            # Listen for speech fragment (short timeout to detect breaks)
            fragment = listen_once(timeout=2, phrase_limit=3)
            
            if fragment:
                # Speech detected - reset silence timer
                accumulated_text += " " + fragment if accumulated_text else fragment
                silence_start_time = None
                log_activity("🎙️  HEARD", fragment)
                last_command_time = time.time()
                print(f"  📝 Building command: {accumulated_text}")
            else:
                # No speech heard in this cycle
                if accumulated_text:
                    # Start timing silence
                    if silence_start_time is None:
                        silence_start_time = time.time()
                    
                    silence_duration = time.time() - silence_start_time
                    
                    # Auto-execute after 2.5 seconds of silence
                    if silence_duration >= SILENCE_TIMEOUT:
                        print(f"\n⏱️  Silence detected ({silence_duration:.1f}s). Executing command...")
                        log_activity("⚙️  EXECUTE", accumulated_text)
                        
                        # Check for stop words
                        if any(sw in accumulated_text.lower() for sw in STOP_WORDS):
                            log_activity("🔴 STOP", f"Stop word detected: '{accumulated_text}'")
                            speak("Going to sleep. Speak again to wake me.")
                            accumulated_text = ""
                            silence_start_time = None
                        else:
                            # Process command in background thread (non-blocking)
                            cmd = accumulated_text.strip()
                            threading.Thread(target=process_command, args=(cmd,), daemon=True).start()
                            accumulated_text = ""
                            silence_start_time = None
                else:
                    # No accumulated text, reset silence timer
                    silence_start_time = None
        
        except KeyboardInterrupt:
            log_activity("🛑 STOP", "Assistant stopped by user")
            print("AIVA stopped.")
            break
        except Exception as e:
            log_activity("❌ ERROR", f"Listening loop error: {e}")
            print(f"[Listening error] {e}")
            time.sleep(0.5)
            accumulated_text = ""
            silence_start_time = None

# ──────────────────────────────────────────────────────────────
# GREETING
# ──────────────────────────────────────────────────────────────
def wish_me():
    h = datetime.datetime.now().hour
    if   h < 12: greeting = "Good Morning!"
    elif h < 18: greeting = "Good Afternoon!"
    else:        greeting = "Good Evening!"
    speak(f"{greeting} Speak your commands and I will execute them automatically.")

# ──────────────────────────────────────────────────────────────
# HELPER UTILITIES
# ──────────────────────────────────────────────────────────────
def open_gmail():
    webbrowser.open("https://mail.google.com")

def open_gmail_compose():
    webbrowser.open("https://mail.google.com/mail/u/0/#inbox?compose=new")

def calculate_expression(expression: str):
    """Safe eval for arithmetic expressions."""
    expression = re.sub(r"[^\d+\-*/().\s]", "", expression).strip()
    if not expression:
        speak("Sorry, I could not find a valid expression.")
        return "No valid expression"
    try:
        result = eval(expression, {"__builtins__": {}}, {})
        answer = f"The result is {result}"
        speak(answer)
        return answer
    except ZeroDivisionError:
        speak("Division by zero is not allowed.")
    except Exception:
        speak("Sorry, I could not calculate that.")
    return "Calculation error"

# Word-based calculator
_ops = {
    "plus": operator.add, "add": operator.add,
    "minus": operator.sub, "subtract": operator.sub,
    "times": operator.mul, "multiplied": operator.mul, "multiply": operator.mul,
    "divided": operator.truediv, "divide": operator.truediv,
}

def word_calculate(query: str) -> str:
    words = query.split()
    for i, w in enumerate(words):
        if w in _ops and i > 0 and i < len(words) - 1:
            try:
                n1 = float(words[i - 1])
                n2 = float(words[i + 1])
                result = _ops[w](n1, n2)
                return f"The result is {result}"
            except Exception:
                pass
    return "Sorry, I could not understand the calculation."

# Gemini AI
genai.configure(api_key=os.environ.get("GOOGLE_API_KEY", ""))

def ai_query(prompt: str) -> str:
    try:
        model = genai.GenerativeModel(
            model_name="gemini-1.5-pro",
            generation_config={"temperature": 0.9, "max_output_tokens": 2048},
            safety_settings=[
                {"category": "HARM_CATEGORY_HARASSMENT",        "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
                {"category": "HARM_CATEGORY_HATE_SPEECH",       "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
                {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
                {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
            ],
        )
        response = model.generate_content([prompt])
        answer = response.text if hasattr(response, "text") else str(response)
        print(f"[AI] {answer[:200]}…")
        speak(answer[:500])
        return answer
    except Exception as e:
        print(f"[AI error] {e}")
        speak("Sorry, I could not get an AI response right now.")
        return "AI error"

# Wikipedia
def search_wikipedia(query: str) -> str:
    try:
        q = re.sub(r"wikipedia", "", query, flags=re.IGNORECASE).strip()
        if not q:
            speak("Please tell me what to search on Wikipedia.")
            return ""
        speak("Searching Wikipedia…")
        result = wikipedia.summary(q, sentences=2)
        speak("According to Wikipedia, " + result)
        return result
    except wikipedia.exceptions.DisambiguationError as e:
        speak("Your query is ambiguous. Please be more specific.")
        return f"Ambiguous: {e.options[:3]}"
    except wikipedia.exceptions.PageError:
        speak("Sorry, I could not find that on Wikipedia.")
        return "Not found"
    except Exception as e:
        speak("Error fetching Wikipedia.")
        print(e)
        return "Error"

# ──────────────────────────────────────────────────────────────
# COMMAND PROCESSOR  —  All commands in one place
# ──────────────────────────────────────────────────────────────
JOKES = [
    "Why do programmers prefer dark mode? Because light attracts bugs.",
    "Why was the computer cold? It forgot to close Windows.",
    "Why do Java developers wear glasses? Because they don't see sharp.",
    "Why did the developer go broke? He used up all his cache.",
    "Why do programmers hate nature? It has too many bugs.",
    "I told my computer I needed a break. Now it won't stop sending me Kit-Kat ads.",
]

def process_command(query: str) -> str:
    """
    Master command dispatcher.
    Every branch returns a non-empty string.
    """
    if not query:
        return "No command."

    query = query.lower().strip()
    response = "Done."

    try:
        # ── Wikipedia ─────────────────────────────────────────
        if "wikipedia" in query:
            response = search_wikipedia(query)

        # ── Time & Date ───────────────────────────────────────
        elif "time" in query:
            t = datetime.datetime.now().strftime("%I:%M %p")
            response = f"The current time is {t}"
            speak(response)

        elif "date" in query:
            d = datetime.date.today().strftime("%B %d, %Y")
            response = f"Today's date is {d}"
            speak(response)

        # ── IP address ────────────────────────────────────────
        elif "ip address" in query or "my ip" in query:
            try:
                ip = requests.get("https://api.ipify.org", timeout=5).text
                response = f"Your public IP address is {ip}"
            except Exception:
                response = "Could not fetch IP. Check your internet connection."
            speak(response)

        # ── YouTube ───────────────────────────────────────────
        elif "search on youtube" in query or "youtube search" in query:
            term = re.sub(r"(search on youtube|youtube search)", "", query).strip()
            webbrowser.open(f"https://www.youtube.com/results?search_query={term}")
            response = f"Searching YouTube for {term}"

        elif "open youtube" in query:
            webbrowser.open("https://www.youtube.com")
            response = "Opened YouTube"

        # ── Gmail ─────────────────────────────────────────────
        elif "compose gmail" in query or "new gmail" in query or "new email" in query:
            open_gmail_compose()
            response = "Opened Gmail compose"

        elif "open gmail" in query:
            open_gmail()
            response = "Opened Gmail"

        # ── Google ────────────────────────────────────────────
        elif "google search" in query:
            term = query.replace("google search", "").strip()
            webbrowser.open(f"https://www.google.com/search?q={term}")
            response = f"Searched Google for {term}"

        elif "open google" in query:
            webbrowser.open("https://www.google.com")
            response = "Opened Google"

        # ── Chrome ────────────────────────────────────────────
        elif "open chrome" in query:
            paths = [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            ]
            opened = False
            for p in paths:
                if os.path.exists(p):
                    subprocess.Popen([p])
                    opened = True
                    break
            if not opened:
                webbrowser.open("https://www.google.com")
            response = "Opened Chrome"

        elif "close chrome" in query:
            os.system("taskkill /f /im chrome.exe")
            response = "Closed Chrome"

        # ── WhatsApp ──────────────────────────────────────────
        elif "open whatsapp" in query or "whatsapp" in query:
            try:
                subprocess.Popen(["explorer", "whatsapp:"])
                response = "Opening WhatsApp"
                speak(response)
            except Exception:
                os.system("start whatsapp:")
                response = "Opening WhatsApp"

        # ── Notepad ───────────────────────────────────────────
        elif "open notepad" in query:
            subprocess.Popen(["notepad.exe"])
            response = "Opened Notepad"

        # ── Calculator ────────────────────────────────────────
        elif "open calculator" in query:
            subprocess.Popen(["calc.exe"])
            response = "Opened Calculator"

        # ── Calculator (arithmetic) ───────────────────────────
        elif "calculate" in query:
            expr = query.replace("calculate", "").strip()
            response = calculate_expression(expr) or word_calculate(query)

        elif any(op in query.split() for op in _ops):
            response = word_calculate(query)
            speak(response)

        # ── Music ─────────────────────────────────────────────
        elif "play music" in query or "play song" in query:
            music_dir = r"D:\SONG"
            if os.path.isdir(music_dir):
                songs = [f for f in os.listdir(music_dir)
                         if f.lower().endswith(('.mp3', '.wav', '.m4a', '.flac'))]
                if songs:
                    os.startfile(os.path.join(music_dir, random.choice(songs)))
                    response = "Playing music"
                else:
                    response = "No music files found in D:\\SONG"
            else:
                response = "Music directory D:\\SONG not found"
            speak(response)

        # ── Volume ────────────────────────────────────────────
        elif "volume up" in query:
            steps = 10
            for _ in range(steps):
                pyautogui.press("volumeup")
                time.sleep(1)
            response = f"Volume increased"

        elif "volume down" in query:
            steps = 10
            for _ in range(steps):
                pyautogui.press("volumedown")
                time.sleep(1)
            response = "Volume decreased"

        elif "mute" in query or "unmute" in query:
            pyautogui.press("volumemute")
            time.sleep(1)
            response = "Toggled mute"

        # ── Window management ─────────────────────────────────
        elif "maximize" in query:
            pyautogui.hotkey('win', 'up')
            time.sleep(1)
            response = "Maximized window"

        elif "minimize" in query:
            pyautogui.hotkey('win', 'down')
            time.sleep(1)
            response = "Minimized window"

        elif "show desktop" in query:
            pyautogui.hotkey('win', 'd')
            time.sleep(1)
            response = "Showing desktop"

        elif "new window" in query:
            pyautogui.hotkey('ctrl', 'n')
            time.sleep(1)
            response = "Created new window"

        elif "incognito" in query or "private" in query:
            pyautogui.hotkey('ctrl', 'shift', 'n')
            time.sleep(1)
            response = "Opened incognito window"

        # ── Tabs ──────────────────────────────────────────────
        elif "new tab" in query:
            pyautogui.hotkey('ctrl', 't')
            time.sleep(1)
            response = "Opened new tab"

        elif "close tab" in query:
            pyautogui.hotkey('ctrl', 'w')
            time.sleep(1)
            response = "Closed tab"

        elif "previous tab" in query:
            pyautogui.hotkey('ctrl', 'shift', 'tab')
            time.sleep(1)
            response = "Previous tab"

        elif "next tab" in query:
            pyautogui.hotkey('ctrl', 'tab')
            time.sleep(1)
            response = "Next tab"

        elif "switch tab" in query or "alt tab" in query:
            pyautogui.hotkey('alt', 'tab')
            time.sleep(1)
            response = "Switched window"

        # ── Browser shortcuts ─────────────────────────────────
        elif "history" in query:
            # pyautogui.hotkey('ctrl', 'h')
            pyautogui.hotkey('ctrl', 'h')
            time.sleep(1)
            response = "Showing history"

        elif "downloads" in query or "download" in query:
            pyautogui.hotkey('ctrl', 'j')
            time.sleep(1)
            response = "Showing downloads"

        elif "clear history" in query or "clear browsing" in query:
            pyautogui.hotkey('ctrl', 'shift', 'delete')
            time.sleep(1)
            response = "Clear browsing dialog opened"

        elif "refresh" in query or "reload" in query:
            pyautogui.press('f5')
            time.sleep(1)
            response = "Refreshed"

        # ── Scroll ────────────────────────────────────────────
        elif "scroll down" in query:
            pyautogui.scroll(-500)
            time.sleep(1)
            response = "Scrolled down"

        elif "scroll up" in query:
            pyautogui.scroll(500)
            time.sleep(1)
            response = "Scrolled up"

        # ── Screenshot ────────────────────────────────────────
        elif "take screenshot" in query or "screenshot" in query:
            try:
                speak("What should I name the screenshot?")
                file_name = listen_once(timeout=6, phrase_limit=4)
                if not file_name:
                    file_name = f"screenshot_{int(time.time())}"
                file_name = re.sub(r'[<>:"/\\|?*\s]', '_', file_name).strip('_') or f"shot_{int(time.time())}"
                time.sleep(0.3)
                img = pyautogui.screenshot()
                save_path = os.path.join(os.path.expanduser("~"), "Desktop", f"{file_name}.png")
                img.save(save_path)
                response = f"Screenshot saved as {file_name}.png on Desktop"
                speak(response)
            except Exception as e:
                print(f"[Screenshot error] {e}")
                response = "Could not take screenshot"
                speak(response)

        # ── Typing ────────────────────────────────────────────
        elif query.startswith("type "):
            text_to_type = query[5:].strip()
            pyautogui.write(text_to_type, interval=0.04)
            response = f"Typed: {text_to_type}"

        # ── File / Folder ─────────────────────────────────────
        elif "new folder" in query:
            pyautogui.hotkey('ctrl', 'shift', 'n')
            time.sleep(1)
            response = "Created new folder"

        elif "save file" in query or "save" in query:
            pyautogui.hotkey('ctrl', 's')
            time.sleep(1)
            time.sleep(1)
            time.sleep(1)
            response = "File saved"

        # ── System power ──────────────────────────────────────
        elif "shutdown" in query or "shut down" in query:
            speak("Shutting down your computer in 5 seconds.")
            time.sleep(5)
            os.system("shutdown /s /t 1")
            response = "Shutting down"

        elif "restart" in query or "reboot" in query:
            speak("Restarting your computer in 5 seconds.")
            time.sleep(5)
            os.system("shutdown /r /t 1")
            response = "Restarting"

        elif "sleep mode" in query or "hibernate" in query:
            speak("Putting your computer to sleep.")
            time.sleep(1)
            os.system("rundll32.exe powrprof.dll,SetSuspendState 0,1,0")
            response = "Sleep mode"

        elif "sign out" in query or "log out" in query:
            speak("Signing out.")
            time.sleep(1)
            os.system("shutdown /l")
            response = "Signing out"

        elif "lock" in query and "screen" in query:
            pyautogui.hotkey('win', 'l')
            time.sleep(1)
            response = "Screen locked"

        # ── AI ────────────────────────────────────────────────
        elif "ask ai" in query or "artificial intelligence" in query or "hey ai" in query:
            prompt = re.sub(r"(ask ai|artificial intelligence|hey ai)", "", query).strip()
            response = ai_query(prompt if prompt else query)

        # ── Personality ───────────────────────────────────────
        elif "who are you" in query:
            response = "I am AIVA, your AI Voice Assistant. I was created to make your life easier!"
            speak(response)

        elif "who created you" in query or "who made you" in query:
            response = "Hiral ma'am and Uchit sir created me using Python in Visual Studio Code."
            speak(response)

        elif "how are you" in query:
            response = "I am doing great, thank you for asking!"
            speak(response)

        elif "what can you do" in query or "your features" in query:
            response = ("I can open apps, search the web and Wikipedia, control your system, "
                        "adjust volume, take screenshots, tell jokes, answer AI questions, "
                        "and much more. Just speak naturally and I'll execute your commands!")
            speak(response)

        elif "who am i" in query:
            response = "You are my creator!"
            speak(response)

        elif "joke" in query:
            response = random.choice(JOKES)
            speak(response)

        elif "hello" in query or "hi" in query:
            response = "Hello! How can I help you today?"
            speak(response)

        elif "good morning" in query or "good afternoon" in query or "good evening" in query:
            response = "Hello! Hope you are having a wonderful day!"
            speak(response)

        elif "thank you" in query or "thanks" in query:
            response = "You're welcome! Is there anything else I can help with?"
            speak(response)

        # ── Generic "open <app>" — MUST be last ──────────────
        elif query.startswith("open "):
            app_name = query.replace("open", "").strip()
            pyautogui.hotkey('win')
            time.sleep(0.8)
            pyautogui.write(app_name, interval=0.05)
            time.sleep(0.6)
            pyautogui.press('enter')
            response = f"Opening {app_name}"

        else:
            # Last resort: send to Gemini AI
            print(f"[Fallback to AI] {query}")
            response = ai_query(query)

    except Exception as e:
        print(f"[Command error] '{query}': {e}")
        response = "I encountered an error. Please try again."
        speak(response)

    return response


# ──────────────────────────────────────────────────────────────
# FLASK ROUTES
# ──────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')


@app.route("/api/command", methods=["POST"])
def api_command():
    try:
        data  = request.get_json(force=True)
        query = data.get("command", "").strip()
        if not query:
            return jsonify({"response": "No command received."}), 400
        print(f"[API] {query}")
        response = process_command(query)
        return jsonify({"response": response, "status": "ok"})
    except Exception as e:
        print(f"[API error] {e}")
        return jsonify({"response": "Error", "error": str(e)}), 500


@app.route("/api/status", methods=["GET"])
def api_status():
    return jsonify({
        "active": assistant_active,
        "listening": True,
        "silence_timeout": SILENCE_TIMEOUT,
        "noise_reduction": NOISE_REDUCE_AVAILABLE,
    })


@app.route("/api/activate", methods=["POST"])
def api_activate():
    """Manually activate/deactivate assistant from web UI."""
    global assistant_active
    data = request.get_json(force=True)
    assistant_active = bool(data.get("active", True))
    status = "activated" if assistant_active else "deactivated"
    speak(f"Assistant {status}.")
    return jsonify({"active": assistant_active})


@app.route("/api/system_status", methods=["GET"])
def system_status():
    cpu    = psutil.cpu_percent(interval=0.5)
    mem    = psutil.virtual_memory()
    disk   = psutil.disk_usage('/')
    bat    = psutil.sensors_battery()

    try:
        temps = psutil.sensors_temperatures()
        temperature = list(temps.values())[0][0].current if temps else None
    except Exception:
        temperature = None

    try:
        local_ip = socket.gethostbyname(socket.gethostname())
    except Exception:
        local_ip = None

    try:
        public_ip = requests.get('https://api.ipify.org', timeout=4).text
    except Exception:
        public_ip = None

    return jsonify({
        "cpu": cpu,
        "memory_percent": mem.percent,
        "memory_total": mem.total,
        "memory_used": mem.used,
        "disk_percent": disk.percent,
        "disk_total": disk.total,
        "disk_used": disk.used,
        "battery": bat.percent if bat else None,
        "temperature": temperature,
        "local_ip": local_ip,
        "public_ip": public_ip,
        "network": "Connected" if local_ip else "Disconnected",
        "time": datetime.datetime.now().strftime("%H:%M:%S"),
        "date": datetime.date.today().strftime("%B %d, %Y"),
    })


@app.route("/who_created_you", methods=["GET"])
def who_created_you():
    return jsonify({
        "message": "Hiral and Uchit sir created me using Python in Visual Studio Code."
    })


def generate_frames():
    camera = cv2.VideoCapture(0)
    try:
        while True:
            ok, frame = camera.read()
            if not ok:
                break
            _, buf = cv2.imencode('.jpg', frame)
            yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + buf.tobytes() + b'\r\n')
    finally:
        camera.release()


@app.route('/video_feed')
def video_feed():
    return Response(generate_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')


@app.route('/api/activity', methods=['GET'])
def api_activity():
    """Return the activity log as JSON."""
    with activity_lock:
        return jsonify({
            "activities": list(activity_log),
            "assistant_active": assistant_active,
            "listening": True,
            "silence_timeout": SILENCE_TIMEOUT
        })


# ──────────────────────────────────────────────────────────────
# ENTRY POINT
# ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("\n" + "═"*60)
    print("   AIVA Voice Assistant v3.0 (Always-On Mode)")
    print(f"   Auto-execute after: {SILENCE_TIMEOUT}s of silence")
    print(f"   Stop words: {', '.join(STOP_WORDS)}")
    print(f"   Noise cancel: {'ON' if NOISE_REDUCE_AVAILABLE else 'OFF (pip install noisereduce)'}")
    print("   Web UI    : http://localhost:5000")
    print("═"*60 + "\n")

    # Start always-listening loop in background thread
    listen_thread = threading.Thread(target=always_listening_loop, daemon=True)
    listen_thread.start()

    # Start Flask (use_reloader=False stops double-thread bug)
    app.run(debug=False, host="0.0.0.0", port=5000, use_reloader=False)