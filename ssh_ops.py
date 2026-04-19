import os
import paramiko
import time
import base64
import io
import socket
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives.asymmetric import rsa

def ensure_ssh_keys():
    """Generates Ed25519 keys (OpenSSH format) and returns Base64 strings."""
    key = ed25519.Ed25519PrivateKey.generate()
    
    private_bytes = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption()
    )
    private_b64 = base64.b64encode(private_bytes).decode('utf-8')
    
    public_bytes = key.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH
    )
    public_ssh = public_bytes.decode('utf-8')
    
    return private_b64, public_ssh

def deploy_to_server(ip, image_tag, env_vars, private_key_str, docker_user=None, docker_token=None, repo_path=None):
    client = None
    try:
        if not private_key_str:
            yield {"status": "error", "message": "No Private Key provided."}
            return

        # --- RECURSIVE KEY DECODER ---
        pem_key_str = None
        candidate = private_key_str.strip()
        for _ in range(3):
            try:
                if "-----BEGIN" in candidate and "PRIVATE KEY-----" in candidate:
                    pem_key_str = candidate
                    break
                clean_candidate = candidate.replace(" ", "").replace("\n", "")
                clean_candidate += "=" * ((4 - len(clean_candidate) % 4) % 4)
                decoded_bytes = base64.b64decode(clean_candidate)
                candidate = decoded_bytes.decode('utf-8')
                if "-----BEGIN" in candidate and "PRIVATE KEY-----" in candidate:
                    pem_key_str = candidate
                    yield {"status": "info", "message": "🔑 Key successfully decoded."}
                    break
            except:
                break

        if not pem_key_str: pem_key_str = private_key_str
        pem_key_str = pem_key_str.replace('\\n', '\n').strip()
        
        if "-----BEGIN" not in pem_key_str:
            pem_key_str = f"-----BEGIN OPENSSH PRIVATE KEY-----\n{pem_key_str}\n-----END OPENSSH PRIVATE KEY-----\n"

        # --- LOAD KEY ---
        pkey = None
        try:
            key_file = io.StringIO(pem_key_str)
            try:
                pkey = paramiko.Ed25519Key.from_private_key(key_file)
            except paramiko.SSHException:
                key_file.seek(0)
                pkey = paramiko.RSAKey.from_private_key(key_file)
        except Exception as e:
            yield {"status": "error", "message": f"Invalid Key: {str(e)}"}
            return

        # --- NETWORK PRE-CHECK ---
        yield {"status": "connecting", "message": f"Checking network route to {ip}:22..."}
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5)
            result = sock.connect_ex((ip.strip(), 22))
            sock.close()
            if result == 0:
                yield {"status": "info", "message": "✅ Port 22 is OPEN and reachable."}
            else:
                yield {"status": "error", "message": f"❌ Port 22 is CLOSED (Error code {result}). Check AWS Security Group."}
                return
        except Exception as e:
            yield {"status": "error", "message": f"❌ Network unreachable: {str(e)}"}
            return

        # --- CONNECT ---
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        max_retries = 3
        retry_delay = 5
        connected = False
        
        for attempt in range(max_retries):
            try:
                # Add banner_timeout to prevent Paramiko from hanging silently
                client.connect(hostname=ip.strip(), username="ubuntu", pkey=pkey, timeout=10, banner_timeout=10)
                connected = True
                break
            except Exception as e:
                err_msg = str(e).replace("\n", " ")
                yield {"status": "info", "message": f"SSH Auth failed (Attempt {attempt+1}/{max_retries}): {err_msg}"}
                time.sleep(retry_delay)
                
        if not connected:
            yield {"status": "error", "message": "❌ SSH Authentication failed permanently. Key mismatch or instance unresponsive."}
            return

        yield {"status": "success", "message": "SSH Connection Established."}
        
        # --- DAEMON CHECK & HEAL ---
        stdin, stdout, stderr = client.exec_command("sudo docker info")
        if stdout.channel.recv_exit_status() != 0:
            yield {"status": "healing", "message": "🛠️ Docker Daemon check failed. Installing Official Version..."}

            env_prefix = "export DEBIAN_FRONTEND=noninteractive && "
            cmds = [
               "sudo rm /var/lib/apt/lists/lock || true",
               "sudo rm /var/cache/apt/archives/lock || true",
               "sudo rm /var/lib/dpkg/lock* || true",
               "sudo apt-get remove -y docker docker.io containerd runc || true",
               f"{env_prefix} sudo apt-get update -qq",
               f"{env_prefix} sudo apt-get install -y ca-certificates curl gnupg -qq",
               "sudo install -m 0755 -d /etc/apt/keyrings",
               "curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg --yes",
               "sudo chmod a+r /etc/apt/keyrings/docker.gpg",
               'echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null',
               f"{env_prefix} sudo apt-get update -qq",
               f"{env_prefix} sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin -qq",
               "sudo systemctl start docker",
               "sudo usermod -aG docker ubuntu"
            ]

            for cmd in cmds:
                yield {"status": "healing", "message": f"running: {cmd[:40]}..."}
                stdin, out, err = client.exec_command(cmd)
                exit_code = out.channel.recv_exit_status()
                if exit_code != 0:
                    yield {"status": "warning", "message": f"Cmd Warning: {err.read().decode()[:50]}"}

            stdin, out, err = client.exec_command("docker --version")
            if out.channel.recv_exit_status() != 0:
                yield {"status": "error", "message": "❌ Docker install failed. Server is broken."}
                return

            yield {"status": "success", "message": "✅ Docker Installed & Verified."}

        # 1. Login
        if docker_user and docker_token:
            yield {"status": "executing", "message": "[remote] 🔐 Authenticating Docker Registry..."}
            command = f"echo '{docker_token}' | sudo docker login -u {docker_user} --password-stdin"
            stdin, stdout, stderr = client.exec_command(command)
            stdout.channel.recv_exit_status()

        # 2. Upload Production Compose
        has_compose = False
        if repo_path and os.path.exists(os.path.join(repo_path, "docker-compose.yml")):
            has_compose = True
            yield {"status": "info", "message": "[remote] 📄 Uploading Production Compose Config..."}
            sftp = client.open_sftp()
            local_compose = os.path.join(repo_path, "docker-compose.yml")
            sftp.put(local_compose, "/home/ubuntu/docker-compose.yml")
            sftp.close()

        # 3. Cleanup & Start
        yield {"status": "executing", "message": "[remote] Restarting Services..."}
        stdin, stdout, stderr = client.exec_command("sudo docker stop $(sudo docker ps -q) || true && sudo docker rm $(sudo docker ps -aq) || true")
        stdout.channel.recv_exit_status()

        env_content = "\n".join([f"{k}={v}" for k, v in env_vars.items()])
        client.exec_command(f"echo '{env_content}' > /home/ubuntu/.env")

        if has_compose:
            yield {"status": "executing", "message": "[remote] 🚀 Starting Multi-Service Stack..."}
            cmd = "cd /home/ubuntu && sudo docker compose pull && sudo docker compose up -d"
        else:
            yield {"status": "executing", "message": "[remote] 🚀 Starting Single Container..."}
            client.exec_command(f"sudo docker pull {image_tag}")
            cmd = f"sudo docker run -d -p 80:80 --env-file /home/ubuntu/.env {image_tag}"

        stdin, stdout, stderr = client.exec_command(cmd)
        exit_code = stdout.channel.recv_exit_status()

        if exit_code == 0:
            yield {"status": "success", "message": "Deployment Complete!"}
        else:
            yield {"status": "error", "message": f"Deployment Failed: {stderr.read().decode()}"}

    except Exception as e:
        yield {"status": "error", "message": f"Deploy Error: {str(e)}"}
    finally:
        if client: client.close()