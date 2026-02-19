#!/bin/bash
# Quick API smoke test
BASE_URL=${1:-http://localhost:8000}

echo "=== TranscodeAPI Smoke Test ==="
echo "Base URL: $BASE_URL"

# Health check
echo -e "\n1. Health check"
curl -s $BASE_URL/health | python3 -m json.tool

# Register user
echo -e "\n2. Register user"
REGISTER=$(curl -s -X POST $BASE_URL/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email": "test@example.com"}')
echo $REGISTER | python3 -m json.tool
API_KEY=$(echo $REGISTER | python3 -c "import sys,json; print(json.load(sys.stdin)['api_key'])" 2>/dev/null)
echo "API Key: $API_KEY"

# Get me
echo -e "\n3. Get user info"
curl -s $BASE_URL/auth/me -H "X-API-Key: $API_KEY" | python3 -m json.tool

# Submit a transcode job (using a small public video)
echo -e "\n4. Submit transcode job"
JOB=$(curl -s -X POST $BASE_URL/jobs \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{
    "input_url": "https://www.w3schools.com/html/mov_bbb.mp4",
    "output_format": "mp4",
    "output_resolution": "640x360"
  }')
echo $JOB | python3 -m json.tool
JOB_ID=$(echo $JOB | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])" 2>/dev/null)
echo "Job ID: $JOB_ID"

# Poll job status
echo -e "\n5. Poll job status (3x with 5s delay)"
for i in 1 2 3; do
  sleep 5
  echo "Attempt $i:"
  curl -s $BASE_URL/jobs/$JOB_ID -H "X-API-Key: $API_KEY" | python3 -m json.tool
done

echo -e "\n=== Done ==="
