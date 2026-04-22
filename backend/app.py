import os
import re
import datetime
from flask import Flask, render_template, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from werkzeug.utils import secure_filename
import threading

# --- App Configuration ---
app = Flask(__name__, template_folder='templates')

# --- Database Configuration ---
# Connection string is taken from environment variable set in docker-compose
db_url = os.environ.get('DATABASE_URL')
print(f"[*] Connecting to Database: {db_url}")

app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = '/app/uploads'
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB limit

db = SQLAlchemy(app)

# --- Database Model ---
class LogEntry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, index=True)
    level = db.Column(db.String(20), index=True)
    message = db.Column(db.Text)
    source_file = db.Column(db.String(100))
    
    def to_dict(self):
        return {
            'id': self.id,
            'timestamp': self.timestamp.strftime('%Y-%m-%d %H:%M:%S') if self.timestamp else 'N/A',
            'level': self.level,
            'message': self.message
        }

# --- THE BRAIN: Error Knowledge Base ---
class KnowledgeBase:
    @staticmethod
    def analyze(message):
        msg_lower = message.lower()
        patterns = [
            (r'connection refused|network is unreachable|no route to host', {
                'reason': 'Network Connectivity Issue',
                'solution': '1. Check if the target service is running.\n2. Check Firewall rules.'
            }),
            (r'permission denied|access denied|eacces', {
                'reason': 'Insufficient Permissions',
                'solution': '1. Run command with elevated privileges (sudo).\n2. Check file ownership.'
            }),
            (r'out of memory|cannot allocate memory|oom killer', {
                'reason': 'System Memory Exhaustion',
                'solution': '1. Check memory usage (free -h).\n2. Kill unnecessary processes.'
            }),
            (r'no space left on device|disk full', {
                'reason': 'Disk Space Exhaustion',
                'solution': '1. Clean up old logs (rm -rf /var/log/*).\n2. Check disk usage (df -h).'
            }),
            (r'port \d+ already in use|address already in use', {
                'reason': 'Port Conflict',
                'solution': '1. Find the process using the port: `lsof -i :<port>`.\n2. Kill the process: `kill -9 <PID>`.'
            }),
            (r'timeout|connection timed out', {
                'reason': 'Network Timeout',
                'solution': '1. Increase timeout duration.\n2. Check network latency.'
            })
        ]

        for pattern, info in patterns:
            if re.search(pattern, msg_lower, re.IGNORECASE):
                return info

        return {
            'reason': 'Unknown Error',
            'solution': '1. Check official documentation.\n2. Search StackOverflow.'
        }

# --- Parsing Logic ---
LOG_REGEX = re.compile(r'(?P<timestamp>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})|\w{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}')
LEVEL_REGEX = re.compile(r'\b(DEBUG|INFO|NOTICE|WARN|WARNING|ERROR|CRIT|CRITICAL|FATAL|TRACE)\b')

def parse_log_line(line):
    match_ts = LOG_REGEX.search(line)
    match_lvl = LEVEL_REGEX.search(line.upper())
    
    ts = None
    lvl = "INFO"
    msg = line.strip()

    if match_ts:
        try:
            ts_str = match_ts.group()
            for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%b %d %H:%M:%S'):
                try:
                    ts = datetime.datetime.strptime(ts_str, fmt)
                    if '%b' in fmt: ts = ts.replace(year=datetime.datetime.now().year)
                    break
                except ValueError: continue
        except: pass
    if match_lvl: lvl = match_lvl.group(1).upper()
    return ts, lvl, msg

def process_file_task(filepath, source_name):
    """Background thread to process file so it doesn't block the HTTP request"""
    with app.app_context():
        batch = []
        BATCH_SIZE = 500
        
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                ts, lvl, msg = parse_log_line(line)
                entry = LogEntry(timestamp=ts, level=lvl, message=msg, source_file=source_name)
                batch.append(entry)
                
                if len(batch) >= BATCH_SIZE:
                    db.session.bulk_save_objects(batch)
                    db.session.commit()
                    batch = []
        
        # Commit remaining
        if batch:
            db.session.bulk_save_objects(batch)
            db.session.commit()
            
        print("[*] Processing Complete")

# --- Routes ---

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files: return jsonify({'error': 'No file'}), 400
    file = request.files['file']
    if file:
        filename = secure_filename(file.filename)
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)
        
        # Spawn background worker
        threading.Thread(target=process_file_task, args=(filepath, filename)).start()
        return jsonify({'message': 'Parsing started...'})

@app.route('/api/logs', methods=['GET'])
def get_logs():
    page = request.args.get('page', 1, type=int)
    per_page = 50
    level_filter = request.args.get('level', 'ALL')
    
    query = LogEntry.query
    if level_filter != 'ALL': query = query.filter_by(level=level_filter)
        
    pagination = query.order_by(LogEntry.id.desc()).paginate(page=page, per_page=per_page)
    return jsonify({
        'logs': [l.to_dict() for l in pagination.items],
        'total': pagination.total
    })

@app.route('/api/analyze', methods=['POST'])
def analyze_log():
    data = request.json
    message = data.get('message', '')
    result = KnowledgeBase.analyze(message)
    return jsonify(result)

@app.route('/api/reset', methods=['POST'])
def reset_db():
    db.session.query(LogEntry).delete()
    db.session.commit()
    return jsonify({'message': 'Database cleared'})

# --- INITIALIZATION ---
# This block creates the table. It MUST run before app.run()
print("[*] Checking Database Tables...")
with app.app_context():
    try:
        db.create_all()
        print("[*] Database Tables Created Successfully!")
    except Exception as e:
        print(f"[!] Error creating tables: {e}")

if __name__ == '__main__':
    # Debug prints to ensure we are using the new code
    print(f"[*] Starting App Version 2.0")
    print(f"[*] UPLOAD FOLDER: {app.config['UPLOAD_FOLDER']}")
    app.run(host='0.0.0.0', port=8000, debug=True)