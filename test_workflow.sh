#!/bin/bash
#
# u1-slicer-bridge Full Workflow Test
# Tests: Upload → Normalize → Bundle → Slice → G-code
#

set -e  # Exit on error

API_URL="http://localhost:8000"
TEST_FILE="${1:-test.3mf}"  # First argument or default to test.3mf
TEST_NAME="test_$(date +%s)"

echo "========================================="
echo "u1-slicer-bridge API Test"
echo "========================================="
echo "API URL: $API_URL"
echo "Test File: $TEST_FILE"
echo "Test Name: $TEST_NAME"
echo ""

# Check if test file exists
if [ ! -f "$TEST_FILE" ]; then
    echo "❌ Error: Test file '$TEST_FILE' not found!"
    echo ""
    echo "Usage: $0 <path-to-3mf-file>"
    echo ""
    echo "Download a test file from MakerWorld:"
    echo "  - https://makerworld.com/en/models/1204272-a-simple-cube-test-print"
    echo "  - https://makerworld.com/en/models/705572-test-cube"
    exit 1
fi

# Color codes for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Helper function to pretty print JSON
pretty_json() {
    python3 -m json.tool 2>/dev/null || cat
}

# Helper function to extract JSON field
extract_field() {
    python3 -c "import sys, json; print(json.load(sys.stdin)$1)" 2>/dev/null
}

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo -e "${BLUE}Step 0: Health Check${NC}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
HEALTH=$(curl -s "$API_URL/healthz")
echo "$HEALTH" | pretty_json
if echo "$HEALTH" | grep -q '"status":"ok"'; then
    echo -e "${GREEN}✓ API is healthy${NC}"
else
    echo -e "${RED}✗ API health check failed${NC}"
    exit 1
fi
echo ""

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo -e "${BLUE}Step 1: Upload 3MF File${NC}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
UPLOAD_RESPONSE=$(curl -s -X POST "$API_URL/upload" \
    -F "file=@$TEST_FILE")

echo "$UPLOAD_RESPONSE" | pretty_json
UPLOAD_ID=$(echo "$UPLOAD_RESPONSE" | extract_field "['upload_id']")

if [ -z "$UPLOAD_ID" ] || [ "$UPLOAD_ID" == "None" ]; then
    echo -e "${RED}✗ Upload failed${NC}"
    exit 1
fi

echo -e "${GREEN}✓ Upload successful${NC}"
echo "  Upload ID: $UPLOAD_ID"
OBJECT_COUNT=$(echo "$UPLOAD_RESPONSE" | extract_field "['objects']" | python3 -c "import sys, json; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "?")
echo "  Objects found: $OBJECT_COUNT"
echo ""

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo -e "${BLUE}Step 2: Initialize Default Filaments${NC}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
FILAMENT_INIT=$(curl -s -X POST "$API_URL/filaments/init-defaults")
echo "$FILAMENT_INIT" | pretty_json
echo ""

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo -e "${BLUE}Step 3: List Available Filaments${NC}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
FILAMENTS=$(curl -s "$API_URL/filaments")
echo "$FILAMENTS" | pretty_json

# Get first filament ID
FILAMENT_ID=$(echo "$FILAMENTS" | extract_field "['filaments'][0]['id']")
echo -e "${GREEN}✓ Using filament ID: $FILAMENT_ID${NC}"
echo ""

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo -e "${BLUE}Step 4: Normalize Objects${NC}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
NORMALIZE_RESPONSE=$(curl -s -X POST "$API_URL/normalize/$UPLOAD_ID" \
    -H "Content-Type: application/json" \
    -d '{"printer_profile": "snapmaker_u1"}')

echo "$NORMALIZE_RESPONSE" | pretty_json

NORM_STATUS=$(echo "$NORMALIZE_RESPONSE" | extract_field "['status']")
if [ "$NORM_STATUS" == "completed" ]; then
    echo -e "${GREEN}✓ Normalization successful${NC}"
else
    echo -e "${RED}✗ Normalization failed: $NORM_STATUS${NC}"

    # Show log file if available
    LOG_PATH=$(echo "$NORMALIZE_RESPONSE" | extract_field "['log_path']")
    if [ ! -z "$LOG_PATH" ] && [ "$LOG_PATH" != "None" ]; then
        echo -e "${YELLOW}Log file:${NC}"
        docker exec u1-slicer-bridge-api-1 cat "$LOG_PATH" 2>/dev/null || echo "  (log not accessible)"
    fi
    exit 1
fi
echo ""

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo -e "${BLUE}Step 5: Get Normalized Objects${NC}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
UPLOAD_DETAILS=$(curl -s "$API_URL/upload/$UPLOAD_ID")
echo "$UPLOAD_DETAILS" | pretty_json

# Extract object IDs
OBJECT_IDS=$(echo "$UPLOAD_DETAILS" | python3 -c "
import sys, json
data = json.load(sys.stdin)
ids = []
for obj in data.get('objects', []):
    # Try different possible ID fields
    obj_id = obj.get('id') or obj.get('object_id')
    if obj_id:
        ids.append(obj_id)
print('[' + ','.join(map(str, ids)) + ']')
" 2>/dev/null || echo "[]")

echo "  Object IDs for bundling: $OBJECT_IDS"
echo ""

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo -e "${BLUE}Step 6: Create Bundle${NC}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
BUNDLE_RESPONSE=$(curl -s -X POST "$API_URL/bundles" \
    -H "Content-Type: application/json" \
    -d "{\"name\": \"$TEST_NAME\", \"object_ids\": $OBJECT_IDS, \"filament_id\": $FILAMENT_ID}")

echo "$BUNDLE_RESPONSE" | pretty_json

BUNDLE_ID=$(echo "$BUNDLE_RESPONSE" | extract_field "['bundle_id']")
if [ -z "$BUNDLE_ID" ] || [ "$BUNDLE_ID" == "None" ]; then
    echo -e "${RED}✗ Bundle creation failed${NC}"
    exit 1
fi

echo -e "${GREEN}✓ Bundle created${NC}"
echo "  Bundle ID: $BUNDLE_ID"
echo ""

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo -e "${BLUE}Step 7: Slice Bundle${NC}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Slicing with:"
echo "  - Layer height: 0.2mm"
echo "  - Infill: 15%"
echo "  - Supports: disabled"
echo ""

SLICE_RESPONSE=$(curl -s -X POST "$API_URL/bundles/$BUNDLE_ID/slice" \
    -H "Content-Type: application/json" \
    -d '{"layer_height": 0.2, "infill_density": 15, "supports": false}')

echo "$SLICE_RESPONSE" | pretty_json

JOB_ID=$(echo "$SLICE_RESPONSE" | extract_field "['job_id']")
SLICE_STATUS=$(echo "$SLICE_RESPONSE" | extract_field "['status']")

if [ -z "$JOB_ID" ] || [ "$JOB_ID" == "None" ]; then
    echo -e "${RED}✗ Slicing job creation failed${NC}"
    exit 1
fi

echo -e "${GREEN}✓ Slicing job created${NC}"
echo "  Job ID: $JOB_ID"
echo "  Status: $SLICE_STATUS"
echo ""

# Check if slicing completed immediately or if we need to poll
if [ "$SLICE_STATUS" == "completed" ]; then
    echo -e "${GREEN}✓ Slicing completed${NC}"
else
    echo -e "${YELLOW}⚠ Slicing may still be processing (status: $SLICE_STATUS)${NC}"
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo -e "${BLUE}Step 8: View Slicing Log${NC}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
LOG_PATH=$(echo "$SLICE_RESPONSE" | extract_field "['log_path']")
if [ ! -z "$LOG_PATH" ] && [ "$LOG_PATH" != "None" ]; then
    echo "Log file: $LOG_PATH"
    echo "Last 30 lines:"
    docker exec u1-slicer-bridge-api-1 tail -n 30 "$LOG_PATH" 2>/dev/null || echo "  (log not accessible)"
else
    echo "  (no log path provided)"
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo -e "${BLUE}Step 9: G-code Metadata${NC}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
GCODE_PATH=$(echo "$SLICE_RESPONSE" | extract_field "['gcode_path']")
if [ ! -z "$GCODE_PATH" ] && [ "$GCODE_PATH" != "None" ]; then
    echo "G-code file: $GCODE_PATH"

    # Extract metadata from response
    PRINT_TIME=$(echo "$SLICE_RESPONSE" | extract_field "['estimated_print_time_seconds']")
    FILAMENT=$(echo "$SLICE_RESPONSE" | extract_field "['filament_used_mm']")
    LAYERS=$(echo "$SLICE_RESPONSE" | extract_field "['total_layers']")

    if [ ! -z "$PRINT_TIME" ] && [ "$PRINT_TIME" != "None" ]; then
        echo "  Print time: ${PRINT_TIME}s ($(($PRINT_TIME / 60))m)"
    fi
    if [ ! -z "$FILAMENT" ] && [ "$FILAMENT" != "None" ]; then
        echo "  Filament: ${FILAMENT}mm"
    fi
    if [ ! -z "$LAYERS" ] && [ "$LAYERS" != "None" ]; then
        echo "  Layers: $LAYERS"
    fi

    # Show first 20 lines of G-code
    echo ""
    echo "First 20 lines of G-code:"
    docker exec u1-slicer-bridge-api-1 head -n 20 "$GCODE_PATH" 2>/dev/null || echo "  (G-code not accessible)"
else
    echo "  (no G-code path provided)"
fi

echo ""
echo "========================================="
echo -e "${GREEN}✓ Full Workflow Test Complete!${NC}"
echo "========================================="
echo ""
echo "Summary:"
echo "  Upload ID:  $UPLOAD_ID"
echo "  Objects:    $OBJECT_COUNT"
echo "  Bundle ID:  $BUNDLE_ID"
echo "  Job ID:     $JOB_ID"
echo "  Status:     $SLICE_STATUS"
echo ""
echo "API Endpoints tested:"
echo "  ✓ POST /upload"
echo "  ✓ GET  /upload/{upload_id}"
echo "  ✓ POST /filaments/init-defaults"
echo "  ✓ GET  /filaments"
echo "  ✓ POST /normalize/{upload_id}"
echo "  ✓ POST /bundles"
echo "  ✓ POST /bundles/{bundle_id}/slice"
echo ""
