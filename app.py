import re
import os
from flask import Flask, request, jsonify, session
from flask_cors import CORS
import requests
import json
from datetime import timedelta
import logging
from urllib.parse import urlparse

# 🔹 Firebase Imports
import firebase_admin
from firebase_admin import credentials, firestore

firebase_json_str = os.environ.get("firebase_credentials")

# 🔹 Firebase Initialization
if firebase_json_str:
    with open("firebase_credentials.json", "w") as f:
        f.write(firebase_json_str)

# Initialize Firebase app
if not firebase_admin._apps:
    cred = credentials.Certificate("firebase_credentials.json")
    firebase_admin.initialize_app(cred)
db = firestore.client()

app = Flask(__name__)

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

app.secret_key = os.environ.get('SECRET_KEY', os.urandom(24))
app.config.update(
    SESSION_COOKIE_NAME='createlo_session',
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    PERMANENT_SESSION_LIFETIME=timedelta(hours=1),
    SESSION_REFRESH_EACH_REQUEST=True
)

allowed_origins = [
    "https://audit.createlo.in",
    "http://localhost:3000",
    "http://localhost:3000/audit-form",
    "http://localhost:3000/business-summary"
]

CORS(app,
     supports_credentials=True,
     resources={
         r"/*": {
             "origins": allowed_origins,
             "methods": ["GET", "POST", "OPTIONS"],
             "allow_headers": ["Content-Type", "Authorization"],
             "expose_headers": ["Content-Type"],
             "max_age": 600
         }
     })

GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')

@app.route('/')
def home():
    return jsonify({"status": "active", "service": "Createlo Audit API"})

@app.route('/submit', methods=['POST', 'OPTIONS'])
def submit():
    if request.method == 'OPTIONS':
        return _build_cors_preflight_response()
    
    try:
        if not request.is_json:
            return jsonify({"error": "Request must be JSON"}), 400
            
        data = request.get_json()
        logger.info(f"Received request with data: {json.dumps(data, indent=2)}")
        
        if not data:
            return jsonify({"error": "No data received"}), 400

        # Validate required fields
        required_fields = ['website', 'email', 'contactNumber']
        missing_fields = [field for field in required_fields if not data.get(field)]
        if missing_fields:
            return jsonify({
                "error": "Missing required fields",
                "missing": missing_fields
            }), 400

        business_url = data.get('website', '')
        if not is_valid_url(business_url):
            return jsonify({"error": "Invalid business URL"}), 400

        # Build the prompt with all available data
        prompt = build_createlo_prompt(
            business_url,
            data.get('email', ''),
            data.get('contactNumber', ''),
            data.get('businessCategory', ''),
            data.get('categoryHint', ''),
            data.get('ownerName', ''),
            data.get('instagram', ''),
            data.get('facebook', '')
        )
        
        if not GEMINI_API_KEY:
            logger.error("Gemini API key not configured")
            return jsonify({"error": "API service unavailable"}), 503
            
        logger.info("Sending request to Gemini API")
        gemini_response = send_to_gemini(prompt)
        
        if isinstance(gemini_response, str) and gemini_response.startswith("Error"):
            logger.error(f"Gemini API error: {gemini_response}")
            return jsonify({"error": gemini_response}), 502

        report_data = extract_report_data(gemini_response)
        if not report_data:
            logger.error("Failed to extract report data from Gemini response")
            return jsonify({
                "error": "Could not generate audit report",
                "details": "Failed to process API response"
            }), 500

        session['report_data'] = report_data
        logger.info("Successfully generated audit report")

        # 🔹 Store user input + AI response in Firebase
        try:
            combined_data = {
                "inputData": data,
                "reportData": report_data
            }
            db.collection("audit_submissions").add(combined_data)
            logger.info("Data successfully stored in Firebase")
        except Exception as firebase_error:
            logger.error(f"Failed to store data in Firebase: {str(firebase_error)}")

        return _corsify_actual_response(jsonify({
            "status": "success",
            "data": report_data
        }))

    except Exception as e:
        logger.error(f"Error in submit endpoint: {str(e)}", exc_info=True)
        return jsonify({
            "error": "Internal server error",
            "details": str(e)
        }), 500

def is_valid_url(url):
    try:
        result = urlparse(url)
        return all([result.scheme, result.netloc])
    except Exception as e:
        logger.error(f"Invalid URL {url}: {str(e)}")
        return False

def build_createlo_prompt(url, email, phone, category=None, category_hint=None, 
                         owner_name=None, instagram=None, facebook=None):
    additional_info = []
    if category:
        additional_info.append(f"Business Category: {category}")
    if category_hint:
        additional_info.append(f"Category Hint: {category_hint}")
    if owner_name:
        additional_info.append(f"Owner Name: {owner_name}")
    if instagram:
        additional_info.append(f"Instagram Handle: {instagram}")
    if facebook:
        additional_info.append(f"Facebook Page: {facebook}")
        
    additional_info_str = "\n".join(additional_info) + "\n" if additional_info else ""
    
    return f"""
You are a digital marketing audit expert working for the Createlo brand. Your goal is to analyze a business's website and provide insights and actionable next steps that highlight opportunities and encourage engagement with Createlo's services.
Business Data:
- URL: {url}
- Email: {email}
- Phone: {phone}
{additional_info_str}

Generate a detailed audit report with the following structure:

const reportData = {{
  // Basic business info inferred from website
  client: "<Business name>",
  businessoverview: "<1-2 sentence overview>",
  
  // Social media analysis (make reasonable assumptions if not provided)
  instagramSummary: "<analysis>",
  facebookSummary: "<analysis>",
  
  // Scores (60-100 range)
  instagramScore: <number>,
  facebookScore: <number>,
  overallScore: <average>,
  
  // Combined summary
  businesssummary: "<10-sentence summary>",
  
  // Marketing insights (4-8 items)
  insights: [
    "<specific insight>",
    "<specific insight>",
    "<specific insight>"
  ],
  
  // Generate several practical and actionable tips derived DIRECTLY from the generated 'insights'. Each tip should identify a specific area for improvement or opportunity related to their online presence (as inferred from the website) and suggest a relevant action. FRAME these tips to naturally lead into recommending a Createlo service (like booking a call, requesting an audit/quote, starting a test campaign) as the solution or next step. Maintain a professional, encouraging, yet action-oriented toneActionable tips (3-5 items)
  tips: [
    "<specific tip mentioning Createlo service>",
    "<specific tip mentioning Createlo service>",
    "<specific tip mentioning Createlo service>"
  ]
}};

IMPORTANT:

1. Maintain EXACT field order as shown above
2. Only return the JavaScript object
3. Scores should be between 60-100
4. Tips should reference Createlo services
5. Make reasonable assumptions for missing info

"""

def extract_report_data(gemini_response):
    try:
        logger.debug("Starting report data extraction")
        
        # First try to parse as pure JSON
        try:
            direct_parse = json.loads(gemini_response)
            if validate_report_data(direct_parse):
                return direct_parse
        except json.JSONDecodeError:
            pass
        
        # If direct parse fails, try extraction patterns
        patterns = [
            r'(?:const|let|var)\s+reportData\s*=\s*({[\s\S]*?})\s*;',
            r'{\s*["\']client["\'][\s\S]*?}',
            r'{[^{}]*}'
        ]
        
        for pattern in patterns:
            match = re.search(pattern, gemini_response, re.DOTALL)
            if match:
                js_object = match.group(1) if match.lastindex else match.group(0)
                logger.debug(f"Found object using pattern: {pattern}")
                
                # Clean and parse
                report_data = clean_json_string(js_object)
                if report_data and validate_report_data(report_data):
                    return report_data
        
        logger.error("No valid JSON object found in response")
        logger.debug(f"Full response:\n{gemini_response}")
        return None

    except Exception as e:
        logger.error(f"Extraction error: {str(e)}", exc_info=True)
        return None

def clean_json_string(js_str):
    """Clean and normalize JSON string with proper quote handling"""
    try:
        # First remove all comments
        cleaned = re.sub(r'/\*.*?\*/', '', js_str, flags=re.DOTALL)
        cleaned = re.sub(r'//.*?$', '', cleaned, flags=re.MULTILINE)
        
        # Handle escaped quotes first by temporarily replacing them
        cleaned = cleaned.replace(r'\"', '%%QUOTE%%')
        
        # Replace all remaining quotes with escaped versions
        cleaned = cleaned.replace('"', r'\"')
        
        # Restore the originally escaped quotes
        cleaned = cleaned.replace('%%QUOTE%%', r'\"')
        
        # Fix property names (ensure they're quoted)
        cleaned = re.sub(r'([{,]\s*)(\w+)\s*:', lambda m: f'{m.group(1)}"{m.group(2)}":', cleaned)
        
        # Remove trailing commas
        cleaned = re.sub(r',\s*([}\]])', r'\1', cleaned)
        
        # Now properly parse the JSON
        # First wrap in quotes to make valid JSON string, then decode
        temp_json = f'"{cleaned}"'
        decoded_str = json.loads(temp_json)
        
        # Now parse the decoded string as JSON
        return json.loads(decoded_str)
        
    except json.JSONDecodeError as e:
        logger.error(f"JSON cleaning failed at position {e.pos}: {str(e)}")
        logger.error(f"Context: {cleaned[max(0,e.pos-30):e.pos+30]}")
        return None
    

def validate_report_data(data):
    """Validate the extracted report data structure and Field orders"""
    required_fields = {
        'client': str,
        'businessoverview': str,
        'instagramSummary': str,
        'facebookSummary': str,
        'instagramScore': (int, float),
        'facebookScore': (int, float),
        'overallScore': (int, float),
        'businesssummary': str,
        'insights': list,
        'tips': list
    }
    
    for field, field_type in required_fields.items():
        if field not in data:
            logger.error(f"Missing required field: {field}")
            return False
        
        if not isinstance(data[field], field_type):
            logger.error(f"Invalid type for {field}: expected {field_type}, got {type(data[field])}")
            return False
    
    # Validate scores
    for score_field in ['instagramScore', 'facebookScore', 'overallScore']:
        if not (0 <= data[score_field] <= 100):
            logger.error(f"Invalid {score_field}: must be between 0-100")
            return False
    
    # Validate lists
    for list_field in ['insights', 'tips']:
        if len(data[list_field]) < 2:
            logger.error(f"{list_field} must have at least 2 items")
            return False
    
    return True

def send_to_gemini(prompt):
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-pro-latest:generateContent?key={GEMINI_API_KEY}"
        headers = {'Content-Type': 'application/json'}
        
        payload = {
            "contents": [{
                "parts": [{
                    "text": prompt + "\n\nRespond ONLY with valid JSON in this exact format:\n\n" + 
                    "{\n" +
                    "  \"client\": \"...\",\n" +
                    "  \"businessoverview\": \"...\",\n" +
                    "  \"instagramSummary\": \"...\",\n" +
                    "  \"facebookSummary\": \"...\",\n" +
                    "  \"instagramScore\": 0,\n" +
                    "  \"facebookScore\": 0,\n" +
                    "  \"overallScore\": 0,\n" +
                    "  \"businesssummary\": \"...\",\n" +
                    "  \"insights\": [\"...\", \"...\"],\n" +
                    "  \"tips\": [\"...\", \"...\"]\n" +
                    "}\n\n" +
                    "No additional text, comments, or explanations."
                }]
            }],
            "safetySettings": [
                {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_ONLY_HIGH"}
            ],
            "generationConfig": {
                "temperature": 0.7,
                "topP": 0.9,
                "topK": 40,
                "response_mime_type": "application/json"
            }
        }

        logger.debug("Sending to Gemini API")
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        response.raise_for_status()
        
        response_json = response.json()
        logger.debug(f"API response: {json.dumps(response_json, indent=2)}")
        
        # Extract response text
        if not response_json.get('candidates'):
            raise ValueError("No candidates in response")
            
        candidate = response_json['candidates'][0]
        if 'content' not in candidate or 'parts' not in candidate['content']:
            raise ValueError("Invalid response structure")
            
        parts = candidate['content']['parts']
        if not parts or 'text' not in parts[0]:
            raise ValueError("No text in response parts")
            
        return parts[0]['text']

    except requests.exceptions.RequestException as e:
        logger.error(f"API request failed: {str(e)}")
        return f"API Error: {str(e)}"
    except Exception as e:
        logger.error(f"Error processing response: {str(e)}")
        return f"Processing Error: {str(e)}"

def _build_cors_preflight_response():
    origin = request.headers.get('Origin')
    if origin not in allowed_origins:
        return jsonify({"error": "Origin not allowed"}), 403
    response = jsonify({"message": "CORS preflight"})
    response.headers.add("Access-Control-Allow-Origin", origin)
    response.headers.add("Access-Control-Allow-Headers", "*")
    response.headers.add("Access-Control-Allow-Methods", "*")
    response.headers.add("Access-Control-Allow-Credentials", "true")
    return response

def _corsify_actual_response(response):
    origin = request.headers.get('Origin')
    if origin in allowed_origins:
        response.headers.add("Access-Control-Allow-Origin", origin)
        response.headers.add("Access-Control-Allow-Credentials", "true")
    return response

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=os.environ.get('DEBUG', 'False') == 'True')
