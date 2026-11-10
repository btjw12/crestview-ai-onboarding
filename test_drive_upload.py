
from google_drive import upload_json

FOLDER_ID = "12f_FHRtb-h96F5lpUN9CRQUS02RvjS_2"

sample_data = {
    "application_id": "TEST-001",
    "client_name": "Demo Client",
    "status": "Processed",
    "risk_score": 25,
    "source": "Monday-FDE"
}

result = upload_json(
    data=sample_data,
    filename="test_application.json",
    folder_id=FOLDER_ID,
)

print("Upload successful:", result["id"])
