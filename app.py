from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client
import anthropic
import gspread
from google.oauth2.service_account import Credentials
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta
import os, json, re
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# ── Clients ──────────────────────────────────────────
claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
twilio = Client(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))
FROM_NUMBER = os.getenv("TWILIO_PHONE")

# ── Google Sheets ─────────────────────────────────────
SCOPES = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = Credentials.from_service_account_file(os.getenv("CREDENTIALS_FILE"), scopes=SCOPES)
gc = gspread.authorize(creds)
sh = gc.open_by_key(os.getenv("GOOGLE_SHEET_ID"))

def get_sheet(name, headers):
    try:
        ws = sh.worksheet(name)
    except:
        ws = sh.add_worksheet(title=name, rows=1000, cols=20)
        ws.append_row(headers)
    return ws

def expenses_ws():
    return get_sheet("Expenses", ["Date","Category","Description","Amount","Currency"])

def reminders_ws():
    return get_sheet("Reminders", ["ID","DateTime","Message","Repeat","Done","UserPhone"])

def debts_ws():
    return get_sheet("Debts", ["Date","Person","Direction","Amount","Reason","Settled"])

def income_ws():
    return get_sheet("Income", ["Date","Source","Amount","Currency","Notes"])

def tasks_ws():
    return get_sheet("Tasks", ["Date","Task","Priority","Done"])

def health_ws():
    return get_sheet("Health", ["Date","Type","Value","Notes"])

# ── Send WhatsApp Message ─────────────────────────────
def send_msg(to, body):
    twilio.messages.create(body=body, from_=FROM_NUMBER, to=f"whatsapp:{to}")

# ── Reminder Scheduler ────────────────────────────────
scheduler = BackgroundScheduler()
scheduler.start()

def check_reminders():
    ws = reminders_ws()
    rows = ws.get_all_records()
    now = datetime.now()
    for i, row in enumerate(rows):
        if row["Done"] == "YES":
            continue
        try:
            rem_time = datetime.strptime(row["DateTime"], "%Y-%m-%d %H:%M")
        except:
            continue
        if now >= rem_time and now < rem_time + timedelta(minutes=2):
            send_msg(row["UserPhone"], f"⏰ *Reminder:* {row['Message']}")
            if row["Repeat"] == "NO":
                ws.update_cell(i + 2, 5, "YES")

# scheduler.add_job(check_reminders, "interval", minutes=1)

# ── System Prompt ─────────────────────────────────────
SYSTEM = """You are Jarvis, Jameel's personal WhatsApp AI assistant based in Multan, Pakistan.
Currency is PKR by default. Be concise, warm, and helpful.

Always respond in this exact JSON format:
{
  "type": "expense|income|reminder|debt|task|health|query|calculator|unclear",
  "reply": "Your WhatsApp reply to user",
  "data": {}
}

Types and their data fields:
- expense: {amount, category, description, currency}
  Categories: Food & Dining, Groceries, Transport, Fuel, Medicine, Home, Education, Shopping, Bills & Utilities, Entertainment, Other
- income: {amount, source, currency, notes}
- reminder: {datetime_str (YYYY-MM-DD HH:MM), message, repeat (YES/NO)}
- debt: {person, direction (i_owe/they_owe), amount, reason}
- task: {task, priority (High/Medium/Low)}
- health: {type (water/meal/weight/sleep/workout/calories), value, notes}
- query: {} — for questions about expenses, debts, reminders, reports
- calculator: {} — for math, currency conversion, split bills
- unclear: {} — if you don't understand

Keep replies short and emoji-friendly for WhatsApp.
Never return anything outside the JSON."""

# ── Build Context for Claude ──────────────────────────
def build_context(user_msg):
    try:
        expenses = expenses_ws().get_all_records()[-20:]
        debts = [r for r in debts_ws().get_all_records() if r["Settled"] == "NO"]
        reminders = [r for r in reminders_ws().get_all_records() if r["Done"] == "NO"]
        tasks = [r for r in tasks_ws().get_all_records() if r["Done"] == "NO"]
    except:
        expenses, debts, reminders, tasks = [], [], [], []

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"""Current datetime: {now}
User message: {user_msg}

Recent expenses (last 20): {json.dumps(expenses)}
Unsettled debts: {json.dumps(debts)}
Pending reminders: {json.dumps(reminders)}
Pending tasks: {json.dumps(tasks)}"""

# ── Process Claude Response ───────────────────────────
def process(parsed, user_phone):
    t = parsed.get("type")
    d = parsed.get("data", {})
    reply = parsed.get("reply", "Done! ✅")

    if t == "expense":
        expenses_ws().append_row([
            datetime.now().strftime("%Y-%m-%d %H:%M"),
            d.get("category", "Other"),
            d.get("description", ""),
            d.get("amount", 0),
            d.get("currency", "PKR")
        ])

    elif t == "income":
        income_ws().append_row([
            datetime.now().strftime("%Y-%m-%d %H:%M"),
            d.get("source", ""),
            d.get("amount", 0),
            d.get("currency", "PKR"),
            d.get("notes", "")
        ])

    elif t == "reminder":
        import uuid
        reminders_ws().append_row([
            str(uuid.uuid4())[:8],
            d.get("datetime_str", ""),
            d.get("message", ""),
            d.get("repeat", "NO"),
            "NO",
            user_phone
        ])

    elif t == "debt":
        debts_ws().append_row([
            datetime.now().strftime("%Y-%m-%d"),
            d.get("person", ""),
            d.get("direction", ""),
            d.get("amount", 0),
            d.get("reason", ""),
            "NO"
        ])

    elif t == "task":
        tasks_ws().append_row([
            datetime.now().strftime("%Y-%m-%d"),
            d.get("task", ""),
            d.get("priority", "Medium"),
            "NO"
        ])

    elif t == "health":
        health_ws().append_row([
            datetime.now().strftime("%Y-%m-%d %H:%M"),
            d.get("type", ""),
            d.get("value", ""),
            d.get("notes", "")
        ])

    elif t == "query":
        pass  # reply already built by Claude

    return reply

# ── Weekly Report ─────────────────────────────────────
def weekly_report():
    try:
        rows = expenses_ws().get_all_records()
        week_ago = datetime.now() - timedelta(days=7)
        weekly = [r for r in rows if datetime.strptime(r["Date"], "%Y-%m-%d %H:%M") >= week_ago]
        total = sum(float(r["Amount"]) for r in weekly)
        by_cat = {}
        for r in weekly:
            by_cat[r["Category"]] = by_cat.get(r["Category"], 0) + float(r["Amount"])
        breakdown = "\n".join([f"  • {k}: PKR {v:,.0f}" for k, v in sorted(by_cat.items(), key=lambda x: -x[1])])
        msg = f"📊 *Weekly Report*\n\nTotal spent: *PKR {total:,.0f}*\n\n{breakdown}\n\n_{len(weekly)} transactions this week_"
        YOUR_NUMBER = os.getenv("YOUR_PHONE", "")
        if YOUR_NUMBER:
            send_msg(YOUR_NUMBER, msg)
    except Exception as e:
        print(f"Weekly report error: {e}")

# scheduler.add_job(weekly_report, "cron", day_of_week="sun", hour=9, minute=0)

# ── Main Webhook ──────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    incoming = request.form.get("Body", "").strip()
    user_phone = request.form.get("From", "").replace("whatsapp:", "")
    resp = MessagingResponse()

    if not incoming:
        resp.message("Hey Jameel! 👋 I'm Jarvis, your personal assistant. How can I help?")
        return str(resp)

    try:
        context = build_context(incoming)
        result = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1000,
            system=SYSTEM,
            messages=[{"role": "user", "content": context}]
        )
        raw = result.content[0].text.strip()
        raw = re.sub(r"```json|```", "", raw).strip()
        parsed = json.loads(raw)
        reply = process(parsed, user_phone)
    except Exception as e:
        print(f"Error: {e}")
        reply = "⚠️ Something went wrong. Please try again!"

    resp.message(reply)
    return str(resp)

@app.route("/", methods=["GET"])
def home():
    return "Jarvis is running! 🤖"

if __name__ == "__main__":
    app.run(debug=True, port=5000)