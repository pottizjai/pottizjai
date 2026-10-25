import os
import time
import re
import random
import threading
from flask import Flask
from pymongo import MongoClient
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from google.auth.transport.requests import Request

# --- Mini Web Server to keep Render Awake ---
app = Flask('')

@app.route('/')
def home():
    return "YouTube Bot is alive!"

def run_web_server():
    app.run(host='0.0.0.0', port=8080)

# Start the web server in a separate background thread
threading.Thread(target=run_web_server, daemon=True).start()
# ---------------------------------------------

# 1. Connect to MongoDB and fetch tokens using Environment Variables
MONGO_URI = os.getenv("MONGO_URI")
if not MONGO_URI:
    print("Error: MONGO_URI secret is not set!", flush=True)
    exit()

db = MongoClient(MONGO_URI)["youtube_bot"]
token_collection = db["tokens"]
chat_history = db["processed_messages_v2"]

token_data = token_collection.find_one({"_id": "youtube_auth"})
if not token_data:
    print("Error: No tokens found in database. Run login.py first.", flush=True)
    exit()

# 2. Rebuild credentials and auto-refresh if expired
creds = Credentials(
    token=token_data["token"],
    refresh_token=token_data["refresh_token"],
    token_uri=token_data["token_uri"],
    client_id=token_data["client_id"],
    client_secret=token_data["client_secret"],
    scopes=token_data["scopes"],
)

if creds.expired and creds.refresh_token:
    creds.refresh(Request())
    token_collection.update_one(
        {"_id": "youtube_auth"}, {"$set": {"token": creds.token}}
    )

youtube = build("youtube", "v3", credentials=creds)


def extract_video_id(url_or_id):
    """Extracts the 11-character video ID from any YouTube URL or raw ID."""
    if len(url_or_id) == 11 and "/" not in url_or_id and "?" not in url_or_id:
        return url_or_id
    patterns = [
        r'(?:v=|\/)([0-9A-Za-z_-]{11}).*',
        r'(?:embed\/)([0-9A-Za-z_-]{11})',
        r'(?:watch\?v=)([0-9A-Za-z_-]{11})',
        r'(?:youtu\.be\/)([0-9A-Za-z_-]{11})'
    ]
    for pattern in patterns:
        match = re.search(pattern, url_or_id)
        if match:
            return match.group(1)
    return url_or_id


def main():
    print("Bot is starting up...\n", flush=True)

    # Fetch the authenticated bot's own channel ID to prevent self-reply loops
    bot_channel_id = None
    try:
        channels_response = youtube.channels().list(part="id", mine=True).execute()
        items = channels_response.get("items", [])
        if items:
            bot_channel_id = items[0]["id"]
            print(f"Authenticated Bot Channel ID: {bot_channel_id}", flush=True)
    except Exception as e:
        print(f"Note: Could not fetch bot channel ID: {e}", flush=True)

    # 3. Get stream link or ID from Render environment variables
    stream_input = os.getenv("STREAM_INPUT")
    if not stream_input:
        print("Error: STREAM_INPUT environment variable is not set!", flush=True)
        return
    stream_input = stream_input.strip()

    video_id = extract_video_id(stream_input)
    print(f"Target Video ID: {video_id}", flush=True)

    live_chat_id = None
    try:
        video_response = youtube.videos().list(
            part="liveStreamingDetails,snippet",
            id=video_id
        ).execute()

        items = video_response.get("items", [])
        if not items:
            print("Error: Video ID not found on YouTube.", flush=True)
            return

        item = items[0]
        print(f"Video Title: {item.get('snippet', {}).get('title')}", flush=True)

        if "liveStreamingDetails" not in item or "activeLiveChatId" not in item["liveStreamingDetails"]:
            print("Error: No active live chat found.", flush=True)
            return

        live_chat_id = item["liveStreamingDetails"]["activeLiveChatId"]
        print(f"Successfully connected to Live Chat ID: {live_chat_id}\n", flush=True)

    except Exception as e:
        print(f"Error fetching live stream details: {e}", flush=True)
        return

    # Ensure a strict unique index on msg_id
    try:
        chat_history.create_index("msg_id", unique=True)
        print("Unique message index created successfully.", flush=True)
    except Exception as e:
        print(f"Index creation note: {e}", flush=True)

    # 4. Continuous Chat Polling Loop
    print("Listening to chat messages... (Press Ctrl+C to stop)\n", flush=True)
    while True:
        try:
            chat_response = (
                youtube.liveChatMessages()
                .list(
                    liveChatId=live_chat_id, part="snippet,authorDetails", maxResults=15
                )
                .execute()
            )

            messages = chat_response.get("items", [])
            polling_interval_ms = chat_response.get("pollingIntervalMillis", 5000)

            for msg in messages:
                msg_id = msg["id"]
                text = msg["snippet"]["displayMessage"]
                author = msg["authorDetails"]["displayName"]
                author_channel_id = msg["authorDetails"].get("channelId")

                # Ignore messages sent by the bot itself
                if bot_channel_id and author_channel_id == bot_channel_id:
                    continue

                print(f"[{author}] says: {text}", flush=True)

                # Try to claim this message in MongoDB to prevent duplicate processing
                try:
                    chat_history.insert_one({"msg_id": msg_id})
                except Exception:
                    # Already processed this message ID
                    continue

                # Bot Commands Logic
                reply_text = None
                clean_text = text.lower().strip()

                if clean_text == "!ping" or clean_text.startswith("!ping "):
                    reply_text = f"Pong! Hello {author}!"
                elif clean_text == "!discord" or clean_text.startswith("!discord "):
                    reply_text = "Join our community Discord server here: discord.gg/example"
                elif clean_text == "!coinflip" or clean_text.startswith("!coinflip "):
                    outcome = random.choice(["Heads", "Tails"])
                    reply_text = f"🪙 Coin Flip Result for {author}: {outcome}!"
                elif clean_text == "!8ball" or clean_text.startswith("!8ball "):
                    responses = [
                        "Yes, definitely!",
                        "As I see it, yes.",
                        "Reply hazy, try again.",
                        "Cannot predict now.",
                        "Don't count on it.",
                        "My sources say no.",
                        "Outlook not so good.",
                        "Very doubtful."
                    ]
                    answer = random.choice(responses)
                    reply_text = f"🔮 Magic 8-Ball says to {author}: {answer}"

                # 5. Send Reply and Handle Errors
                if reply_text:
                    try:
                        youtube.liveChatMessages().insert(
                            part="snippet",
                            body={
                                "snippet": {
                                    "liveChatId": live_chat_id,
                                    "type": "textMessageEvent",
                                    "textMessageDetails": {"messageText": reply_text},
                                }
                            },
                        ).execute()
                        print(f"--> Successfully Replied to {author}: {reply_text}", flush=True)
                    except Exception as e:
                        print(f"Error sending YouTube reply: {e}", flush=True)

            # Respect YouTube's recommended polling rate
            time.sleep(polling_interval_ms / 1000.0)

        except Exception as e:
            print(f"Error processing chat: {e}", flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
