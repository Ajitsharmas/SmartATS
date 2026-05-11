# ----------------------------------------------------------------------------------------------------------------------------------------------
# Purpose: Load Testing Script 
# ----------------------------------------------------------------------------------------------------------------------------------------------

from locust import HttpUser, task, between

class CandidateUser(HttpUser):
    wait_time: between(1,3)
    token = None

    def on_start(self):
        """
        Before hitting the endpoints, the user must log in.
        For recruiter, we use the recruiter credentials created earlier
        """

        response = self.client.post(
            "/token",
            data={"username": "abc@example.com", "password": "123456"},
        )

        if response.status_code == 200:
            self.token = response.json().get("access_token")
            # Set the header for all future requests
            self.headers = {"Authorization": f"Bearer  {self.token}"}
        else:
            print(f"Login Failed: {response.text}")
    

    @task
    def upload_resume(self):
        if not self.token:
            return

        # OPEN THE REAL FILE IN BINARY MODE
        # Make sure 'test_cv.pdf is in the same folder as this Script
        try:
            with open("test_cv.pdf", "rb") as f:
                files = {"file": ("test_cv.pdf", f, "application/pdf")}
                self.client.post("/upload", files=files,headers=self.headers)
        except FileNotFoundError:
            print(
                "❌Error: tyest_cv.pdf not found. Please add a valid PDF to the folder."
            )
            self.stop(True) # Stop the runner if file is missing
    
    @task(3)
    def view_jobs(self):
        # Even vewing jobs might be public, but let's test the secured path is applicable
        self.client.get("/jobs")