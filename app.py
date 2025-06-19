import re
import os
from flask import Flask, request, jsonify, session
from flask_cors import CORS
import requests
import json
from datetime import timedelta
import logging
from urllib.parse import urlparse
import firebase_admin
from firebase_admin import credentials, firestore
import uuid

# Setup logging
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# Initialize Firebase with in-memory JSON
firebase_json_str = os.environ.get("FIREBASE_CREDENTIALS")
if firebase_json_str and not firebase_admin._apps:
    try:
        cred_dict = json.loads(firebase_json_str)
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        logger.info("✅ Firebase initialized and Firestore client created")
    except Exception as e:
        logger.error(f"🔥 Firebase initialization failed: {str(e)}")
        db = None

app = Flask(__name__)

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

        # Validate required fields (website is not required)
        required_fields = ['email', 'contactNumber']
        missing_fields = [field for field in required_fields if not data.get(field)]
        if missing_fields:
            return jsonify({
                "error": "Missing required fields",
                "missing": missing_fields
            }), 400

        business_url = data.get('website', '')
        if business_url and not is_valid_url(business_url):
            return jsonify({"error": "Invalid business URL"}), 400

        # instagram_url = data.get('instagram', '')
        # if instagram_url and not is_valid_url(instagram_url):
        #     return jsonify({"error": "Invalid Instagram URL"}), 400

        # facebook_url = data.get('facebook', '')
        # if facebook_url and not is_valid_url(facebook_url):
        #     return jsonify({"error": "Invalid Facebook URL"}), 400

        # Build the prompt with all available data
        prompt = build_createlo_prompt(
            data.get('businessName', ''),
            business_url,
            instagram_url,
            facebook_url,
            data.get('email', ''),
            data.get('contactNumber', ''),
            data.get('businessDescription', '')
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

        # Store report data in session
        session['report_data'] = report_data
        logger.info("Successfully generated audit report")

        # Store user input + AI response in Firebase
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
    if not url:
        return True  # Empty URL is valid since website is optional
    try:
        result = urlparse(url)
        return all([result.scheme, result.netloc])
    except Exception as e:
        logger.error(f"Invalid URL {url}: {str(e)}")
        return False

def build_createlo_prompt(business_name, website, instagram, facebook, email, phone, business_description):
    additional_info = []
    if business_name:
        additional_info.append(f"Business Name: {business_name}")
    if business_description:
        additional_info.append(f"Business Description: {business_description}")
    if email:
        additional_info.append(f"Email: {email}")
    if phone:
        additional_info.append(f"Phone: {phone}")
    if website:
        additional_info.append(f"Website URL: {website}")
    if instagram:
        additional_info.append(f"Instagram URL: {instagram}")
    if facebook:
        additional_info.append(f"Facebook URL: {facebook}")
        
    additional_info_str = "\n".join(additional_info) + "\n" if additional_info else ""
    
    return f"""
You are a digital marketing audit expert working for the Createlo brand. Your task is to analyze a business’s online presence and return a detailed audit report in structured JavaScript format. Use the provided business information to assess their website (if provided) and social media channels. Provide valuable insights and actionable tips to improve their digital marketing effectiveness and encourage engagement with Createlo services.

Scoring & Validation Rules:
1. If the website URL is valid and reachable:
   * Calculate total score based on:
     * Website: 50%
     * Instagram: 25%
     * Facebook: 25%
   * Each individual score (websiteScore, instagramScore, facebookScore) should be between 60 and 100, and overallScore should reflect the weighted average.
2. If the website is missing or unreachable:
   * Only use Instagram and Facebook for scoring.
   * In this case:
     * Instagram: 50%
     * Facebook: 50%
   * Final scores (instagramScore, facebookScore, overallScore) should fall between 60 and 100, but the overallScore must be reduced by 50% to reflect the missing website weight.
   * Additionally, generate multiple relevant tips and suggestions encouraging the business to build or improve their website presence and clearly recommend Createlo services for doing so.
3. If any social media channel is not found, state "Not found" in the summary and assign a default minimum score of 60 to that channel.

Input Fields:
{additional_info_str}

Output Format: Return a clean, valid JavaScript object with this exact field order:
const reportData = {{
  client: "<Business name>",
  businessoverview: "<Short description of business>",
  instagramSummary: "<analysis or 'Not found'>",
  facebookSummary: "<analysis or 'Not found'>",
  instagramScore: <number>, // 60–100
  facebookScore: <number>, // 60–100
  websiteScore: <number>, // 60–100 (if applicable)
  overallScore: <number>, // weighted calculation based on rules
  businesssummary: "<10-sentence overview of their digital presence, performance on each platform, website quality, engagement opportunities, and areas for growth>",
  insights: [
    "<Insight about marketing or digital gaps>",
    "<Insight about engagement opportunity>",
    "<Insight about platform-specific performance>"
  ],
  tips: [
    "<Unlimited actionable tips with Createlo call to action>",
    "<Each tip should map to an insight and include a suggestion>",
    "<If website is missing, give strong web-building tips and recommend Createlo services>"
  ]
}};

IMPORTANT:
1. Maintain EXACT field order as shown above
2. Only return the JavaScript object
3. Scores should be between 60-100
4. Tips should reference Createlo services
5. Make reasonable assumptions for missing info
6. If website is missing, include websiteScore as null
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
    try:
        # Remove comments
        cleaned = re.sub(r'/\*.*?\*/', '', js_str, flags=re.DOTALL)
        cleaned = re.sub(r'//.*?$', '', cleaned, flags=re.MULTILINE)
        
        # Handle escaped quotes
        cleaned = cleaned.replace(r'\"', '%%QUOTE%%')
        cleaned = cleaned.replace('"', r'\"')
        cleaned = cleaned.replace('%%QUOTE%%', r'\"')
        
        # Fix property names
        cleaned = re.sub(r'([{,]\s*)(\w+)\s*:', lambda m: f'{m.group(1)}"{m.group(2)}":', cleaned)
        
        # Remove trailing commas
        cleaned = re.sub(r',\s*([}\]])', r'\1', cleaned)
        
        # Parse JSON
        temp_json = f'"{cleaned}"'
        decoded_str = json.loads(temp_json)
        return json.loads(decoded_str)
        
    except json.JSONDecodeError as e:
        logger.error(f"JSON cleaning failed at position {e.pos}: {str(e)}")
        logger.error(f"Context: {cleaned[max(0,e.pos-30):e.pos+30]}")
        return None

def validate_report_data(data):
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
    for score_field in ['instagramScore', 'facebookScore']:
        if not (60 <= data[score_field] <= 100):
            logger.error(f"Invalid {score_field}: must be between 60-100")
            return False
    
    if not (0 <= data['overallScore'] <= 100):
        logger.error("Invalid overallScore: must be between 0-100")
        return False
    
    if 'websiteScore' in data and data['websiteScore'] is not None and not (60 <= data['websiteScore'] <= 100):
        logger.error("Invalid websiteScore: must be between 60-100 or null")
        return False
    
    # Validate lists
    for list_field in ['insights', 'tips']:
        if len(data[list_field]) < 3:
            logger.error(f"{list_field} must have at least 3 items")
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
                    "  \"websiteScore\": null,\n" +
                    "  \"overallScore\": 0,\n" +
                    "  \"businesssummary\": \"...\",\n" +
                    "  \"insights\": [\"...\", \"...\", \"...\"],\n" +
                    "  \"tips\": [\"...\", \"...\", \"...\"]\n" +
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
