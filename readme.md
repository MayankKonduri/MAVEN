# MAVEN Development & Deployment Workflow

## Project Location

**Active MAVEN project directory:**

```bash
/home/mayankkonduri/MAVEN_CLEAN
```

This is the only directory that should be used for development.

**Do NOT edit files inside:**

```bash
/home/mayankkonduri/MAVEN
/home/mayankkonduri/MAVEN_BACKUP
```

These directories are legacy backups from the Git corruption recovery and are not connected to the active system services.

---

# Making Code Changes

1. Open and edit files inside:

```bash
/home/mayankkonduri/MAVEN_CLEAN
```

Example:

```bash
/home/mayankkonduri/MAVEN_CLEAN/voice_assistant.py
```

2. Save your changes.

---

# Restarting MAVEN Services

After modifying a file, restart the corresponding service so the Raspberry Pi loads the updated code.

## Voice Assistant

```bash
sudo systemctl restart maven-voice.service
```

## Camera Server

```bash
sudo systemctl restart maven-camera.service
```

## Microphone Server

```bash
sudo systemctl restart maven-mic.service
```

## IR Server

```bash
sudo systemctl restart maven.service
```

## Restart All MAVEN Services

```bash
sudo systemctl restart maven.service maven-camera.service maven-mic.service maven-voice.service
```

---

# Checking Service Status

Check all MAVEN services:

```bash
systemctl status maven.service maven-camera.service maven-mic.service maven-voice.service
```

---

# Viewing Logs

## Voice Assistant Logs

```bash
journalctl -u maven-voice.service -f
```

## Camera Server Logs

```bash
journalctl -u maven-camera.service -f
```

## Microphone Server Logs

```bash
journalctl -u maven-mic.service -f
```

## IR Server Logs

```bash
journalctl -u maven.service -f
```

Press `Ctrl + C` to exit the live log view.

---

# Saving Changes to GitHub

After your changes have been tested and verified:

```bash
cd ~/MAVEN_CLEAN

git status
git add .
git commit -m "Describe what you changed"
git push
```

This updates the GitHub repository:

```
MayankKonduri/MAVEN
```

---

# Important Notes

* The Raspberry Pi `systemd` services are configured to run code from:

```bash
/home/mayankkonduri/MAVEN_CLEAN
```

* Editing files in the old `MAVEN` directory will have **no effect** on the running system.

* Always test changes locally by restarting the appropriate service before committing and pushing to GitHub.

* Keep `MAVEN_BACKUP` as a temporary safety copy until the new system has been fully validated.
