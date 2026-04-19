import docker
import json
import os

client = docker.from_env()

def login_docker(username, token):
    try:
        client.login(username=username, password=token)
        return {"status": "success", "message": "Docker Login Successful"}
    except Exception as e:
        return {"status": "error", "message": f"Docker Login Failed: {str(e)}"}

def write_dockerfile(repo_path, content):
    dockerfile_path = os.path.join(repo_path, "Dockerfile")
    with open(dockerfile_path, "w") as f:
        f.write(content)
    return dockerfile_path

def build_image(context_path, tag_name, dockerfile="Dockerfile", docker_user=None, docker_token=None):
    """
    Builds a Docker image from a specific CONTEXT folder.
    """
    try:
        if docker_user and docker_token:
            login_docker(docker_user, docker_token)

        yield {"status": "building", "message": f"🔨 Building {tag_name} from context: {os.path.basename(context_path)}..."}

        image, build_logs = client.images.build(
            path=context_path,
            dockerfile=dockerfile,
            tag=tag_name,
            rm=True
        )

        for chunk in build_logs:
            try:
                if isinstance(chunk, bytes):
                    chunk = chunk.decode('utf-8')
                for line in chunk.splitlines():
                    if not line.strip(): continue
                    data = json.loads(line)
                    if 'stream' in data:
                        yield {"status": "building", "message": data['stream'].strip()}
                    if 'error' in data:
                        yield {"status": "error", "message": data['error']}
                        return
            except: pass

        yield {"status": "success", "message": f"Successfully tagged {tag_name}"}

    except Exception as e:
        yield {"status": "error", "message": f"Build Exception: {str(e)}"}

def push_image(tag_name, docker_user, docker_token):
    try:
        yield {"status": "pushing", "message": f"🚀 Pushing {tag_name}..."}
        push_logs = client.images.push(tag_name, stream=True, decode=True)
        for chunk in push_logs:
            if 'status' in chunk:
                msg = chunk['status']
                if 'progress' in chunk: msg += f" {chunk['progress']}"
                yield {"status": "pushing", "message": msg}
            if 'error' in chunk:
                yield {"status": "error", "message": chunk['error']}
                return
        yield {"status": "success", "message": f"Push complete: {tag_name}"}
    except Exception as e:
        yield {"status": "error", "message": f"Push Exception: {str(e)}"}
