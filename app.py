import os
import json
import requests
import fitz
from flask import Flask, render_template, jsonify
from bs4 import BeautifulSoup
from apscheduler.schedulers.background import BackgroundScheduler
import datetime
from pydantic import BaseModel, Field
app = Flask(__name__)
app.config['DOWNLOAD_FOLDER'] = 'downloads'
os.makedirs(app.config['DOWNLOAD_FOLDER'], exist_ok=True)

GEMINI_API_KEY_MAIN = "AQ.Ab8RN6JL_U-QhZRztOiKknDbfypwT40Vv1Xcz2tGz56o7-kv_w"
GEMINI_API_KEY_FALLBACK = "AIzaSyBcH_BNdBihpbinfTx8P1k8fztYFD663hk"
DATA_FILE = "reports_db.json"

TARGET_URLS = [
    "https://t.me/s/researchreportss",
    "https://t.me/s/Equity_Insights",
    "https://www.cnbc.com/finance/",
    "https://economictimes.indiatimes.com/markets/stocks/recos"
]

# We force Gemini to output perfect JSON so we can track and replace easily!
class Report(BaseModel):
    source_id: int = Field(description="The exact integer ID of the source text this was extracted from")
    firm_name: str = Field(description="Name of the research firm. Use 'Not Specified' if missing.")
    company: str = Field(description="Company the report is about")
    headline: str = Field(description="Headline or title")
    date: str = Field(description="Date of the report. Use 'Not Specified' if missing.")
    rating: str = Field(description="Stock rating (e.g. Buy, Sell, Hold). Use 'Not Specified' if missing.")
    price_target: str = Field(description="Price target. Use 'Not Specified' if missing.")
    target_timeframe: str = Field(description="Time period to achieve target (e.g. 1 Year, 12-18 months). Use 'Not Specified' if missing.")
    key_takeaways: list[str] = Field(description="1-3 bullet points of summary context")
    key_metrics: list[str] = Field(description="Any other financial metrics mentioned")

class ReportExtraction(BaseModel):
    reports: list[Report]

def extract_with_gemini(batched_data, use_fallback=False):
    api_key = GEMINI_API_KEY_FALLBACK if use_fallback else GEMINI_API_KEY_MAIN
    
    if not batched_data:
        return []
        
    prompt = f"Extract the financial reports from the following JSON array of sources. For each report extracted, you MUST return the exact 'source_id' it came from so I can track the URL. If any field is not explicitly mentioned in the text, use 'Not Specified' instead of 'N/A' or leaving it blank. Specifically look closely for the 'target_timeframe' (e.g., '12 months', '1 year') and 'price_target'. CRITICAL: In equity research, price targets are almost universally 12-month targets. If a 'price_target' is found but the timeframe is not explicitly stated in the text, you MUST default 'target_timeframe' to '12 Months'.\nSources: {json.dumps(batched_data)[:60000]}"
    
    schema = {"type": "object", "properties": {"reports": {"type": "array", "items": {"properties": {"source_id": {"type": "integer"}, "firm_name": {"type": "string"}, "company": {"type": "string"}, "headline": {"type": "string"}, "date": {"type": "string"}, "rating": {"type": "string"}, "price_target": {"type": "string"}, "target_timeframe": {"type": "string"}, "key_takeaways": {"items": {"type": "string"}, "type": "array"}, "key_metrics": {"items": {"type": "string"}, "type": "array"}}, "required": ["source_id", "firm_name", "company", "headline", "date", "rating", "price_target", "target_timeframe", "key_takeaways", "key_metrics"], "type": "object"}}}}
    
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={api_key}"
    headers = {"Content-Type": "application/json"}
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "responseSchema": schema}
    }

    try:
        response = requests.post(url, headers=headers, json=payload)
        if response.status_code == 200:
            data = response.json()
            text = data['candidates'][0]['content']['parts'][0]['text']
            return json.loads(text).get("reports", [])
        else:
            print(f"Gemini API error: {response.status_code} {response.text}", flush=True)
            if not use_fallback:
                print("Switching to fallback key...", flush=True)
                return extract_with_gemini(batched_data, use_fallback=True)
            return []
    except Exception as e:
        print(f"Error calling Gemini via HTTP: {e}", flush=True)
        if not use_fallback:
            print("Switching to fallback key...", flush=True)
            return extract_with_gemini(batched_data, use_fallback=True)
        return []

def automated_scraping_job():
    print(f"[{datetime.datetime.now()}] Starting hourly tracking job...", flush=True)
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, 'r') as f:
            db = json.load(f)
    else:
        db = {"last_updated": "Never", "reports": {}}
        
    reports_dict = db.get("reports", {})
    
    items_to_process = []
    current_id = 0
    url_map = {}
    
    for url in TARGET_URLS:
        try:
            # 1. Telegram scraping
            if "t.me/s/" in url:
                print(f"Scraping Telegram: {url}", flush=True)
                response = requests.get(url, headers=headers, timeout=15)
                soup = BeautifulSoup(response.text, 'html.parser')
                
                messages = soup.find_all('div', class_='tgme_widget_message')
                valid_messages = []
                for msg in reversed(messages):
                    text_div = msg.find('div', class_='tgme_widget_message_text')
                    if text_div:
                        valid_messages.append(msg)
                    if len(valid_messages) >= 10:
                        break
                
                for msg in valid_messages:
                    text_div = msg.find('div', class_='tgme_widget_message_text')
                    if not text_div: continue
                    text = text_div.get_text(separator=' ', strip=True)
                    
                    link_tag = msg.find('a', class_='tgme_widget_message_date')
                    post_url = link_tag['href'] if link_tag else url
                    
                    is_pdf = bool(msg.find('a', class_='tgme_widget_message_document_wrap'))
                    source_type = "pdf" if is_pdf else "post"
                    
                    current_id += 1
                    items_to_process.append({"source_id": current_id, "text": text})
                    url_map[current_id] = {"url": post_url, "type": source_type}
                    
            # 2. Website/News Deep Article Scraping
            else:
                print(f"Scraping News Hub: {url}", flush=True)
                response = requests.get(url, headers=headers, timeout=15)
                soup = BeautifulSoup(response.text, 'html.parser')
                
                # Find deep article links instead of just the hub page
                article_links = []
                for a in soup.find_all('a', href=True):
                    href = a['href']
                    if len(href) > 40 and ('article' in href or '.html' in href or '.cms' in href or '/news/' in href or '/recos/' in href):
                        if href.startswith('/'): 
                            domain = '/'.join(url.split('/')[:3])
                            href = domain + href
                        if href not in article_links and href.startswith('http'):
                            article_links.append(href)
                
                if not article_links:
                    article_links = [url]
                    
                # Fetch up to 4 articles from this hub
                for article_url in article_links[:4]:
                    try:
                        res = requests.get(article_url, headers=headers, timeout=10)
                        s = BeautifulSoup(res.text, 'html.parser')
                        text = " ".join([p.get_text(strip=True) for p in s.find_all('p')])
                        
                        current_id += 1
                        items_to_process.append({"source_id": current_id, "text": text[:3000]})
                        url_map[current_id] = {"url": article_url, "type": "website"}
                    except Exception as e:
                        pass
                
        except Exception as e:
            print(f"Error scraping {url}: {e}", flush=True)

    # Process ALL items in ONE API call to avoid hitting the 20 requests per day limit!
    if items_to_process:
        print(f"Batching {len(items_to_process, flush=True)} sources into ONE Gemini API call...")
        extracted = extract_with_gemini(items_to_process)
        
        for rep in extracted:
            company = rep.get("company", "Unknown")
            source_id = rep.get("source_id")
            if company == "Unknown" or source_id not in url_map: continue
            
            item_info = url_map[source_id]
            existing = reports_dict.get(company)
            should_update = False
            
            # PRIORITY OVERWRITE LOGIC
            if not existing:
                should_update = True
            elif existing['source_type'] in ['post', 'website'] and item_info['type'] == 'pdf':
                should_update = True
            elif existing['source_type'] == item_info['type']:
                should_update = True 
                
            if should_update:
                rep['source_url'] = item_info['url']
                rep['source_type'] = item_info['type']
                rep['tracked_at'] = str(datetime.datetime.now()) # <-- Track timestamp to sort reverse chronologically!
                reports_dict[company] = rep
                print(f"Tracked/Updated {company} ({item_info['type']})", flush=True)

    db['last_updated'] = str(datetime.datetime.now())
    db['reports'] = reports_dict
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(db, f, indent=4)
    print("Job complete. Tracker database saved.", flush=True)

# Start background thread immediately for initial run
threading.Thread(target=automated_scraping_job, daemon=True).start()

scheduler = BackgroundScheduler()
scheduler.add_job(func=automated_scraping_job, trigger="interval", minutes=60)
scheduler.start()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/get_latest', methods=['GET'])
def get_latest():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, 'r', encoding='utf-8') as f:
            return jsonify(json.load(f))
    return jsonify({"reports": {}, "last_updated": "Never"})

