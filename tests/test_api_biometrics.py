

def test_the_demo_warns_that_an_enrolment_will_not_survive(client, monkeypatch):
    """A free tier wipes the disk, and face photographs are the data.

    Losing somebody's enrolment silently is worse than telling them first,
    so the deployment declares it and the client renders a warning.
    """
    client.post("/api/auth/register",
                json={"username": "ephemeral", "password": "pw12345"})
    token = client.post("/api/auth/login",
                        json={"username": "ephemeral",
                              "password": "pw12345"}).json()["access_token"]
    headers = {"Authorization": "Bearer " + token}

    assert client.get("/api/biometrics/status",
                      headers=headers).json()["storage_is_ephemeral"] is False

    monkeypatch.setenv("FACE_STORAGE_EPHEMERAL", "1")
    assert client.get("/api/biometrics/status",
                      headers=headers).json()["storage_is_ephemeral"] is True
