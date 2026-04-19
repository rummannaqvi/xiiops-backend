import os
import re
import uvicorn
import uuid
import boto3
import json
import zipfile
import git
import paramiko
import secrets
import logging
import hmac
import hashlib
import shutil

from pathlib import Path
from io import BytesIO
from datetime import datetime, timedelta
from dotenv import load_dotenv

from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

# --- GEMINI / LANGCHAIN IMPORTS ---
from langchain_google_vertexai import ChatVertexAI
from langchain_core.prompts import PromptTemplate, ChatPromptTemplate, MessagesPlaceholder
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage

# --- AGENT IMPORTS (from Project B) ---
from langchain.agents import create_tool_calling_agent, AgentExecutor
from langchain_core.runnables.history import RunnableWithMessageHistory
from langchain_community.chat_message_histories import PostgresChatMessageHistory

# --- Internal Modules ---
from docker_ops import write_dockerfile, build_image, push_image, login_docker
from ssh_ops import ensure_ssh_keys, deploy_to_server
from utils import clone_repo, get_file_tree
from infra_ops import write_terraform_file, run_terraform_command, run_terraform_destroy
from tools import analyze_github_repo, save_infrastructure_code, execute_terraform_plan, read_local_file, save_cicd_workflow
from database import init_db

# --- SILENCE PARAMIKO NOISE ---
logging.getLogger("paramiko").setLevel(logging.WARNING)

load_dotenv()

app = FastAPI(title="XiiOps Engine", version="2.0.0")

# --- INITIALIZE DB ON STARTUP ---
@app.on_event("startup")
def on_startup():
    init_db()

# --- GLOBAL STATE (IN-MEMORY) ---
PIPELINE_STATE = {}

def update_pipeline_log(repo_url, message, status=None):
    key = repo_url.lower().strip()
    if key not in PIPELINE_STATE:
        PIPELINE_STATE[key] = {"status": "idle", "logs": [], "updated_at": datetime.utcnow()}

    timestamp = datetime.now().strftime("%H:%M:%S")
    formatted_msg = f"[{timestamp}] {message}"
    PIPELINE_STATE[key]["logs"].append(formatted_msg)
    PIPELINE_STATE[key]["updated_at"] = datetime.utcnow()

    if status:
        PIPELINE_STATE[key]["status"] = status

    print(message)

# --- CORS ---
origins = ["http://localhost:3000", "http://127.0.0.1:3000"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- GEMINI LLM (replaces OpenAI in Project A) ---
llm = ChatVertexAI(
    model="gemini-2.5-pro",
    location="us-east1",
    project="project-20a5d45f-d677-4558-a04",
)

# ============================================================
# SECTION 1: REQUEST MODELS
# ============================================================

class RepoRequest(BaseModel):
    repo_url: str
    git_token: str = None

class BuildRequest(BaseModel):
    repo_url: str
    analysis_json: dict
    docker_username: str
    docker_token: str = None
    git_token: str = None

class InfraRequest(BaseModel):
    repo_url: str
    analysis_json: dict
    aws_access_key: str
    aws_secret_key: str
    region: str = "us-east-1"
    instance_type: str = "t3.micro"
    instance_name: str = "xiiops-server"
    git_token: str = None

class DeployRequest(BaseModel):
    public_ip: str
    docker_username: str
    repo_url: str
    env_vars: dict = {}
    private_key: str = None
    docker_token: str = None
    git_token: str = None

class MetricsRequest(BaseModel):
    public_ip: str
    aws_access_key: str
    aws_secret_key: str
    region: str = "us-east-1"

class LogsRequest(BaseModel):
    public_ip: str
    docker_username: str
    repo_url: str
    lines: int = 100

class ExportRequest(BaseModel):
    repo_url: str
    git_token: str = None

class GitHubPushRequest(BaseModel):
    repo_url: str
    github_token: str
    git_token: str = None

class PipelineStatusRequest(BaseModel):
    repo_url: str

# --- From Project B ---
class ChatRequest(BaseModel):
    message: str
    session_id: str = "default_session"

# ============================================================
# SECTION 2: TERRAFORM STATE STORAGE
# ============================================================

STATE_STORAGE_DIR = "terraform_states"
if not os.path.exists(STATE_STORAGE_DIR):
    os.makedirs(STATE_STORAGE_DIR)

def get_state_file_path(repo_url):
    safe_name = hashlib.md5(repo_url.encode()).hexdigest()
    return os.path.join(STATE_STORAGE_DIR, f"{safe_name}.tfstate")

# ============================================================
# SECTION 3: PROJECT REGISTRY (CI/CD Brain)
# ============================================================

REGISTRY_FILE = "project_registry.json"

# Safe fix: if it's a directory, use a different filename
if os.path.isdir(REGISTRY_FILE):
    REGISTRY_FILE = "project_registry_data.json"

def save_project_to_registry(repo_url, data):
    registry = {}
    if os.path.exists(REGISTRY_FILE):
        try:
            with open(REGISTRY_FILE, "r") as f:
                registry = json.load(f)
        except:
            pass
    key = repo_url.lower().strip()
    registry[key] = data
    with open(REGISTRY_FILE, "w") as f:
        json.dump(registry, f, indent=4)
    print(f"💾 Project registered for CI/CD: {key}")

def delete_project_from_registry(repo_url):
    if not os.path.exists(REGISTRY_FILE):
        return
    try:
        with open(REGISTRY_FILE, "r") as f:
            registry = json.load(f)
        key = repo_url.lower().strip()
        deleted = False
        if key in registry:
            del registry[key]; deleted = True
        if not deleted and key + ".git" in registry:
            del registry[key + ".git"]; deleted = True
        if not deleted and key.replace(".git", "") in registry:
            del registry[key.replace(".git", "")]; deleted = True
        if deleted:
            with open(REGISTRY_FILE, "w") as f:
                json.dump(registry, f, indent=4)
            print(f"🗑️ Project removed from registry: {key}")
    except Exception as e:
        print(f"⚠️ Failed to clean registry: {e}")

def get_project_from_registry(repo_url):
    if not os.path.exists(REGISTRY_FILE):
        return None
    try:
        with open(REGISTRY_FILE, "r") as f:
            registry = json.load(f)
        key = repo_url.lower().strip()
        if key in registry: return registry[key]
        if key + ".git" in registry: return registry[key + ".git"]
        if key.replace(".git", "") in registry: return registry[key.replace(".git", "")]
        return None
    except:
        return None

# ============================================================
# SECTION 4: HELPER FUNCTIONS
# ============================================================

def scan_for_env_vars(repo_path: str):
    relevant_content = ""
    priority_files = [".env.example", ".env.template", "docker-compose.yml", "docker-compose.yaml", "config.js", "settings.py"]
    code_extensions = {".js", ".ts", ".py", ".go", ".java", ".php"}

    for root, _, files in os.walk(repo_path):
        for file in files:
            if file in priority_files:
                try:
                    with open(os.path.join(root, file), 'r', errors='ignore') as f:
                        relevant_content += f"\n--- FILE: {file} ---\n{f.read()}\n"
                except:
                    pass

    scan_limit = 50000
    current_size = len(relevant_content)

    for root, _, files in os.walk(repo_path):
        if "node_modules" in root or ".git" in root or "venv" in root:
            continue
        for file in files:
            ext = os.path.splitext(file)[1]
            if ext in code_extensions and current_size < scan_limit:
                try:
                    path = os.path.join(root, file)
                    with open(path, 'r', errors='ignore') as f:
                        content = f.read()
                        if "process.env" in content or "os.environ" in content or "getenv" in content:
                            relevant_content += f"\n--- SOURCE: {file} ---\n{content[:2000]}\n"
                            current_size += len(content[:2000])
                except:
                    pass

    return relevant_content

def list_directory_tool(repo_path: str, subpath: str = "."):
    target_path = os.path.normpath(os.path.join(repo_path, subpath))
    if not target_path.startswith(repo_path): return "Error: Access Denied"
    try:
        if not os.path.exists(target_path): return "Error: Path not found"
        items = []
        for root, dirs, files in os.walk(target_path):
            level = root.replace(target_path, '').count(os.sep)
            if level > 1: continue
            indent = ' ' * 4 * level
            items.append(f"{indent}{os.path.basename(root)}/")
            subindent = ' ' * 4 * (level + 1)
            for f in files:
                items.append(f"{subindent}{f}")
        return "\n".join(items[:50])
    except Exception as e:
        return f"Error listing directory: {str(e)}"

def read_file_tool(repo_path: str, filepath: str):
    target_path = os.path.normpath(os.path.join(repo_path, filepath))
    if not target_path.startswith(repo_path): return "Error: Access Denied"
    try:
        with open(target_path, "r", errors='ignore') as f:
            return f.read(4000)
    except Exception as e:
        return f"Error reading file: {str(e)}"

# ============================================================
# SECTION 5: CI/CD PIPELINE EXECUTION
# ============================================================

def run_cicd_pipeline(repo_url: str, pusher_name: str, commit_message: str):
    update_pipeline_log(repo_url, f"🚀 [CI/CD] Triggered by {pusher_name}: {commit_message}", "running")

    project_data = get_project_from_registry(repo_url)
    if not project_data:
        update_pipeline_log(repo_url, "❌ [CI/CD] Failed: Project not registered. Deploy manually first.", "error")
        return

    update_pipeline_log(repo_url, "✅ Credentials found. Starting Pipeline...", "running")

    ip = project_data['public_ip']
    docker_user = project_data['docker_username']
    docker_token = project_data.get('docker_token')
    git_token = project_data.get('git_token')
    private_key = project_data['private_key']
    env_vars = project_data.get('env_vars', {})

    try:
        repo_path = clone_repo(repo_url, git_token)
        repo_name = os.path.basename(repo_path).replace(".git", "").lower()
        full_image_tag = f"{docker_user}/{repo_name}:latest"

        if not os.path.exists(os.path.join(repo_path, "Dockerfile")):
            update_pipeline_log(repo_url, "⚠️ No Dockerfile found. Aborting.", "error")
            return

        if docker_token:
            login_docker(docker_user, docker_token)

        update_pipeline_log(repo_url, f"🐳 Building Image: {full_image_tag}...")
        for chunk in build_image(repo_path, tag_name=full_image_tag, docker_user=docker_user, docker_token=docker_token):
            if chunk.get("status") == "error":
                update_pipeline_log(repo_url, f"❌ Build Error: {chunk.get('message')}", "error")
                return

        update_pipeline_log(repo_url, f"📤 Pushing Image...")
        for chunk in push_image(full_image_tag, docker_user, docker_token):
            if chunk.get("status") == "error":
                update_pipeline_log(repo_url, f"❌ Push Error: {chunk.get('message')}", "error")
                return

        update_pipeline_log(repo_url, f"🚀 Deploying to {ip}...")
        for chunk in deploy_to_server(ip, full_image_tag, env_vars, private_key_str=private_key, docker_user=docker_user, docker_token=docker_token):
            msg = chunk.get('message', '')
            if msg: update_pipeline_log(repo_url, f"-> {msg}")

        update_pipeline_log(repo_url, f"✨ Pipeline Complete! App updated on {ip}", "success")

    except Exception as e:
        update_pipeline_log(repo_url, f"❌ Critical Failure: {str(e)}", "error")

# ============================================================
# SECTION 6: AGENT SETUP (from Project B, using Gemini)
# ============================================================

DB_URL = os.getenv("DATABASE_URL")

agent_system_prompt = """You are XiiOps, an expert AI-driven Platform Engineer and Site Reliability Engineer (SRE).
Your core philosophy is the "Glass Box" approach: you generate highly transparent, portable Infrastructure-as-Code (Terraform/OpenTofu).
Your goal is to assist developers in analyzing repositories, generating CI/CD workflows, and provisioning AWS resources.
Communicate directly, technically, and concisely."""

agent_prompt = ChatPromptTemplate.from_messages([
    ("system", agent_system_prompt),
    MessagesPlaceholder(variable_name="chat_history"),
    ("user", "{user_input}"),
    MessagesPlaceholder(variable_name="agent_scratchpad"),
])

agent_tools = [analyze_github_repo, save_infrastructure_code, execute_terraform_plan, read_local_file, save_cicd_workflow]
agent = create_tool_calling_agent(llm, agent_tools, agent_prompt)

# FIX 1: Turn off verbose mode to stop the StdOutCallbackHandler AttributeError spam
agent_executor = AgentExecutor(agent=agent, tools=agent_tools, verbose=False)

# FIX 2: Create a wrapper to patch empty Gemini tool-call messages
class SafePostgresHistory(PostgresChatMessageHistory):
    @property
    def messages(self):
        # Fetch the actual messages from the database
        msgs = super().messages
        # Patch empty AI messages with a space so Gemini doesn't crash with a 400 error
        for msg in msgs:
            if msg.type == "ai" and not msg.content:
                msg.content = " "
        return msgs

def get_session_history(session_id: str):
    if not DB_URL:
        # Fallback: in-memory history if no DB configured
        from langchain_community.chat_message_histories import ChatMessageHistory
        return ChatMessageHistory()

    # Use the new Safe wrapper here instead of the default PostgresChatMessageHistory
    history = SafePostgresHistory(
        connection_string=DB_URL,
        session_id=session_id,
        table_name="chat_history"
    )
    messages = history.messages
    if len(messages) > 40:
        history.clear()
        for msg in messages[-40:]:
            history.add_message(msg)
    return history

agent_with_memory = RunnableWithMessageHistory(
    agent_executor,
    get_session_history,
    input_messages_key="user_input",
    history_messages_key="chat_history",
)

def process_chat(user_input: str, session_id: str = "default_session") -> str:
    try:
        response = agent_with_memory.invoke(
            {"user_input": user_input},
            config={"configurable": {"session_id": session_id}}
        )
        output = response.get("output", "")
        if isinstance(output, list):
            text_parts = [item["text"] for item in output if isinstance(item, dict) and "text" in item]
            return "".join(text_parts)
        return str(output)
    except Exception as e:
        return f"System Error: Unable to reach AI core. Details: {str(e)}"

def fetch_history(session_id: str) -> list:
    history = get_session_history(session_id)
    chat_history = []
    for msg in history.messages:
        if msg.type == "human":
            chat_history.append({"role": "user", "content": str(msg.content)})
        elif msg.type == "ai":
            content = msg.content
            if isinstance(content, list):
                content = "".join([item.get("text", "") for item in content if isinstance(item, dict) and "text" in item])
            else:
                content = str(content)
            if content.strip():
                chat_history.append({"role": "ai", "content": content})
    return chat_history

# ============================================================
# SECTION 7: ROUTES — HEALTH & INFO
# ============================================================

@app.get("/")
def health_check():
    return {"system": "XiiOps Engine", "status": "operational", "version": "2.0.0", "ai_backend": "gemini-2.5-pro"}

# ============================================================
# SECTION 8: ROUTES — CHAT AGENT (from Project B)
# ============================================================

@app.get("/health")
def health():
    return {"status": "healthy", "service": "XiiOps Backend", "message": "AI DevOps Agent is ready."}

@app.post("/api/chat")
def chat_with_agent(request: ChatRequest):
    """Conversational AI agent endpoint (Project B feature)."""
    reply = process_chat(request.message, request.session_id)
    return {"reply": reply}

@app.get("/api/history/{session_id}")
def get_chat_history(session_id: str):
    """Load past messages for a session."""
    messages = fetch_history(session_id)
    return {"messages": messages}

# ============================================================
# SECTION 9: ROUTES — REPO ANALYSIS & BUILD (from Project A)
# ============================================================

@app.post("/api/v1/analyze")
def analyze_repo(payload: RepoRequest):
    try:
        repo_path = clone_repo(payload.repo_url, payload.git_token)
        file_structure = get_file_tree(repo_path)
        code_snippets = scan_for_env_vars(repo_path)

        prompt = PromptTemplate.from_template(
            """
            You are a Senior DevOps Architect.

            File Structure:
            {file_structure}

            Code Snippets & Config Files:
            {code_snippets}

            YOUR MISSION:
            1. Identify the Language & Framework.
            2. Determine the Deployment Strategy.
            3. DETECT ENVIRONMENT VARIABLES: Scan for `process.env.VAR`, `os.environ['VAR']`, `getenv('VAR')` or keys in .env.example.
               - Ignore standard system vars like NODE_ENV, PORT unless critical.
               - Look specifically for Secrets, API Keys, DB URIs.

            Return a JSON object with keys:
            "language", "framework", "suggested_port", "deployment_strategy",
            "detected_env_vars": ["LIST", "OF", "DETECTED", "VARIABLE_NAMES"]

            Return RAW JSON only. No markdown formatting.
            """
        )

        chain = prompt | llm
        response = chain.invoke({
            "file_structure": file_structure,
            "code_snippets": code_snippets
        })

        # Gemini may wrap in markdown code blocks — strip them
        cleaned_content = response.content.strip().replace("```json", "").replace("```", "").strip()

        return {"status": "success", "repo": payload.repo_url, "analysis": cleaned_content}

    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/v1/generate-build")
async def generate_and_build(payload: BuildRequest):
    try:
        repo_path = clone_repo(payload.repo_url, payload.git_token)
        repo_name = os.path.basename(repo_path).lower().replace("_", "-")

        if not os.getenv("GOOGLE_API_KEY"):
            raise Exception("GOOGLE_API_KEY is missing")

        agent_llm = ChatVertexAI(
            model="gemini-2.5-pro",
            location="us-east1",
            project="project-20a5d45f-d677-4558-a04",
            )
            
    except Exception as e:
        return StreamingResponse(
            iter([json.dumps({"status": "error", "message": f"Init Failed: {str(e)}"}) + "\n"]),
            media_type="application/x-ndjson"
        )

    system_prompt = f"""
    You are a Principal DevOps Architect.

    CONTEXT:
    - User: {payload.docker_username}
    - Repo: {repo_name}

    MISSION:
    1. Analyze the repository.
    2. Define a Multi-Service Build Strategy.

    RULES:
    1. Context Awareness: Identify the subdirectory for each service (e.g., './frontend').
    2. Multi-Stage Builds: Dockerfiles MUST be multi-stage to avoid needing local 'dist' folders.
    3. Production Compose: The final docker-compose.yml MUST use 'image:' tags, NOT 'build:'.

    OUTPUT JSON:
    {{
        "strategy": "compose",
        "services": [
            {{
                "name": "frontend",
                "build_context": "./frontend",
                "dockerfile_path": "Dockerfile",
                "dockerfile_content": "FROM node..."
            }}
        ],
        "docker_compose_prod": "version: '3'..."
    }}

    Protocol: ACTION: LIST <path> | ACTION: READ <path> | ACTION: GENERATE <json>
    """

    def log_streamer():
        try:
            if payload.docker_token:
                yield json.dumps({"status": "info", "message": "🔐 Authenticating..."}) + "\n"
                login_docker(payload.docker_username, payload.docker_token)

            history = [SystemMessage(content=system_prompt)]
            history.append(HumanMessage(content=f"Repo cloned. Root: {list_directory_tool(repo_path)}"))

            final_plan = None
            max_steps = 25
            step = 0

            yield json.dumps({"status": "generating", "message": "🤖 Architecting..."}) + "\n"

            while step < max_steps:
                print(f"--- STEP {step} ---")
                response = agent_llm.invoke(history)
                msg = response.content.strip()
                print(f"🤖 AGENT: {msg[:100]}...")

                if "ACTION: LIST" in msg:
                    path = msg.split("ACTION: LIST")[-1].strip()
                    yield json.dumps({"status": "generating", "message": f"👀 Analyzing: '{path}'..."}) + "\n"
                    result = list_directory_tool(repo_path, path)
                    history.append(AIMessage(content=msg))
                    history.append(HumanMessage(content=f"Result:\n{result}"))
                elif "ACTION: READ" in msg:
                    path = msg.split("ACTION: READ")[-1].strip()
                    yield json.dumps({"status": "generating", "message": f"📖 Validating '{path}'..."}) + "\n"
                    result = read_file_tool(repo_path, path)
                    history.append(AIMessage(content=msg))
                    history.append(HumanMessage(content=f"Content:\n{result}"))
                elif "ACTION: GENERATE" in msg:
                    json_str = msg.split("ACTION: GENERATE")[-1].strip().replace("```json", "").replace("```", "")
                    try:
                        final_plan = json.loads(json_str)
                        yield json.dumps({"status": "success", "message": "📝 Blueprints Approved."}) + "\n"
                        break
                    except Exception as e:
                        history.append(HumanMessage(content=f"JSON Error: {e}. Retry."))
                else:
                    history.append(HumanMessage(content="Please use a valid ACTION: LIST, READ, or GENERATE."))
                step += 1

            if not final_plan:
                yield json.dumps({"status": "error", "message": "❌ Agent failed to produce a plan."}) + "\n"
                return

            strategy = final_plan.get("strategy", "single")
            services = final_plan.get("services", [])

            yield json.dumps({"status": "info", "message": f"🚀 Executing Strategy: {strategy.upper()}"}) + "\n"

            for svc in services:
                svc_name = svc['name']
                rel_context = svc.get('build_context', '.')
                abs_context = os.path.join(repo_path, rel_context)
                df_content = svc.get('dockerfile_content')

                target_df_path = os.path.join(abs_context, "Dockerfile")
                os.makedirs(os.path.dirname(target_df_path), exist_ok=True)
                with open(target_df_path, "w") as f:
                    f.write(df_content)

                tag = f"{payload.docker_username}/{repo_name}-{svc_name}:latest"
                if strategy == 'single':
                    tag = f"{payload.docker_username}/{repo_name}:latest"

                yield json.dumps({"status": "building", "message": f"🐳 Building {svc_name}..."}) + "\n"

                chunk_success = True
                last_error = ""
                for chunk in build_image(abs_context, tag_name=tag, dockerfile="Dockerfile",
                                         docker_user=payload.docker_username, docker_token=payload.docker_token):
                    yield json.dumps(chunk) + "\n"
                    if chunk.get("status") == "error":
                        chunk_success = False
                        last_error = chunk.get("message")

                if not chunk_success:
                    yield json.dumps({"status": "error", "message": f"❌ Build Failed: {last_error}"}) + "\n"
                    return

                yield json.dumps({"status": "pushing", "message": f"📤 Pushing {tag}..."}) + "\n"
                for chunk in push_image(tag, payload.docker_username, payload.docker_token):
                    yield json.dumps(chunk) + "\n"
                    if chunk.get("status") == "error": return

            if strategy == "compose":
                prod_compose = final_plan.get("docker_compose_prod", "")

                if not prod_compose or "build:" in prod_compose:
                    yield json.dumps({"status": "info", "message": "🔒 Enforcing Production Config..."}) + "\n"

                with open(os.path.join(repo_path, "docker-compose.yml"), "w") as f:
                    f.write(prod_compose)

                with open(os.path.join(repo_path, "docker-compose.yml"), "r") as f:
                    content = f.read()

                if "build:" in content:
                    yield json.dumps({"status": "healing", "message": "🩹 Patching Compose File..."}) + "\n"
                    lines = content.splitlines()
                    new_lines = []
                    current_service = None
                    for line in lines:
                        if "services:" in line:
                            new_lines.append(line)
                            continue
                        m = re.match(r'^  (\w+):', line)
                        if m: current_service = m.group(1)
                        if "build:" in line:
                            if current_service:
                                img = f"{payload.docker_username}/{repo_name}-{current_service}:latest"
                                new_lines.append(f"    image: {img}")
                            else:
                                new_lines.append("    # Build removed")
                        elif "volumes:" in line and "./" in line:
                            new_lines.append("    # Volume removed")
                        else:
                            new_lines.append(line)
                    with open(os.path.join(repo_path, "docker-compose.yml"), "w") as f:
                        f.write("\n".join(new_lines))

            yield json.dumps({"status": "success", "message": "Artifacts Ready."}) + "\n"

        except Exception as e:
            yield json.dumps({"status": "error", "message": f"Pipeline Error: {str(e)}"}) + "\n"

    return StreamingResponse(log_streamer(), media_type="application/x-ndjson")


# ============================================================
# SECTION 10: ROUTES — INFRASTRUCTURE (from Project A)
# ============================================================

@app.post("/api/v1/generate-infra")
async def generate_and_provision(payload: InfraRequest):
    repo_path = clone_repo(payload.repo_url, payload.git_token)
    private_key_b64, public_key_ssh = ensure_ssh_keys()

    prompt = PromptTemplate.from_template(
        """
        You are a Terraform Expert. Write a 'main.tf' for AWS.

        Requirements:
        1. Provider: "aws", Region: "{region}".
        2. Key Pair: Create 'aws_key_pair' named "{key_name}" using public_key: "{public_key}".
        3. Security Group: Create 'aws_security_group' allowing HTTP (80), HTTPS (443), SSH (22), 3000, 8000. Name prefix "xiiops_sg_".
        4. Resource: Create 'aws_instance' named "xiiops_instance".
           - AMI: 'ami-0c7217cdde317cfec' (Ubuntu 22.04 us-east-1)
           - Instance Type: "{instance_type}"
           - Key Name: "{key_name}"
           - vpc_security_group_ids: [aws_security_group.<YOUR_SG_NAME>.id]
           - Tags: Name = "{instance_name}"
           - User Data: None
        5. Output: 'public_ip'.

        RETURN CODE ONLY. NO MARKDOWN.
        """
    )


    def log_streamer():
        env_vars = os.environ.copy()
        env_vars.update({
            "AWS_ACCESS_KEY_ID": payload.aws_access_key,
            "AWS_SECRET_ACCESS_KEY": payload.aws_secret_key,
            "AWS_DEFAULT_REGION": payload.region
        })

        attempt = 0
        max_retries = 5
        key_name = f"xiiops_auth_{uuid.uuid4().hex[:6]}"
        success = False

        while attempt < max_retries:
            if attempt > 0:
                yield json.dumps({"status": "healing", "message": f"♻️ Key conflict. Retrying with new name: '{key_name}'..."}) + "\n"
            else:
                yield json.dumps({"status": "generating", "message": "🤖 Generating Terraform config..."}) + "\n"

            chain = prompt | llm
            tf_content = chain.invoke({
                "analysis": json.dumps(payload.analysis_json),
                "region": payload.region,
                "public_key": public_key_ssh,
                "key_name": key_name,
                "instance_type": payload.instance_type,
                "instance_name": payload.instance_name
            })

            clean_content = tf_content.content.replace("```hcl", "").replace("```terraform", "").replace("```", "").strip()
            write_terraform_file(repo_path, clean_content)

            if attempt == 0:
                yield json.dumps({"status": "init", "message": "Initializing Terraform..."}) + "\n"
                for line in run_terraform_command(repo_path, "init", env_vars):
                    yield json.dumps({"status": "init", "message": line}) + "\n"

            yield json.dumps({"status": "apply", "message": "Provisioning Server..."}) + "\n"
            retry_needed = False
            last_ip = None

            for line in run_terraform_command(repo_path, "apply", env_vars):
                if "public_ip =" in line:
                    try:
                        last_ip = line.split("=")[1].strip().replace('"', '')
                    except:
                        pass

                if "InvalidKeyPair.Duplicate" in line or "already exists" in line:
                    retry_needed = True
                    continue

                yield json.dumps({"status": "apply", "message": line}) + "\n"

            if retry_needed:
                key_name = f"xiiops_auth_{uuid.uuid4().hex[:6]}"
                attempt += 1
                continue

            if not last_ip:
                yield json.dumps({"status": "error", "message": "Terraform finished but no IP returned."}) + "\n"
                return

            success = True

            state_src = os.path.join(repo_path, "terraform.tfstate")
            state_dst = get_state_file_path(payload.repo_url)
            if os.path.exists(state_src):
                shutil.copy(state_src, state_dst)
                yield json.dumps({"status": "info", "message": "💾 Terraform State backed up."}) + "\n"

            yield json.dumps({
                "status": "success",
                "message": "Infrastructure Ready.",
                "public_ip": last_ip,
                "private_key": private_key_b64
            }) + "\n"
            break

        if not success:
            yield json.dumps({"status": "error", "message": "Provisioning failed after max retries."}) + "\n"

    return StreamingResponse(log_streamer(), media_type="application/x-ndjson")


@app.post("/api/v1/destroy-infra")
async def destroy_infra(payload: InfraRequest):
    try:
        delete_project_from_registry(payload.repo_url)
        repo_path = clone_repo(payload.repo_url, payload.git_token)

        def log_streamer():
            try:
                env_vars = os.environ.copy()
                env_vars.update({
                    "AWS_ACCESS_KEY_ID": payload.aws_access_key,
                    "AWS_SECRET_ACCESS_KEY": payload.aws_secret_key,
                    "AWS_DEFAULT_REGION": payload.region
                })

                state_backup = get_state_file_path(payload.repo_url)
                state_dest = os.path.join(repo_path, "terraform.tfstate")

                if os.path.exists(state_backup):
                    shutil.copy(state_backup, state_dest)
                    yield json.dumps({"status": "info", "message": "♻️ Terraform State Restored."}) + "\n"
                else:
                    yield json.dumps({"status": "warning", "message": "⚠️ No State backup found. Destroy might fail."}) + "\n"

                provider_tf = f'provider "aws" {{\n  region = "{payload.region}"\n}}\n'
                write_terraform_file(repo_path, provider_tf)


                yield json.dumps({"status": "destroying", "message": "💥 INITIATING DESTRUCT SEQUENCE..."}) + "\n"

                for line in run_terraform_destroy(repo_path, env_vars):
                    yield json.dumps({"status": "destroying", "message": line}) + "\n"

                if os.path.exists(state_backup):
                    os.remove(state_backup)

                yield json.dumps({"status": "success", "message": "Infrastructure Destroyed. Registry Cleared."}) + "\n"

            except Exception as inner_e:
                yield json.dumps({"status": "error", "message": f"Terraform Error: {str(inner_e)}"}) + "\n"

        return StreamingResponse(log_streamer(), media_type="application/x-ndjson")

    except Exception as e:
        return StreamingResponse(
            iter([json.dumps({"status": "error", "message": f"Setup Failed: {str(e)}"}) + "\n"]),
            media_type="application/x-ndjson"
        )

# ============================================================
# SECTION 11: ROUTES — DEPLOY, METRICS, LOGS
# ============================================================

@app.post("/api/v1/deploy")
async def deploy_app(payload: DeployRequest):
    print(f"\n🔑 DEBUG: Received Key: {payload.private_key[:50] if payload.private_key else 'NULL/EMPTY'}...")

    save_project_to_registry(payload.repo_url, {
        "public_ip": payload.public_ip,
        "docker_username": payload.docker_username,
        "env_vars": payload.env_vars,
        "private_key": payload.private_key,
        "docker_token": payload.docker_token,
        "git_token": payload.git_token
    })

    repo_name = payload.repo_url.split("/")[-1].replace(".git", "").lower()
    full_image_tag = f"{payload.docker_username}/{repo_name}:latest"

    def log_streamer():
        yield json.dumps({"status": "starting", "message": "🕵️ Agent scanning for missing secrets..."}) + "\n"

        repo_path = None
        final_env_vars = payload.env_vars.copy()

        try:
            repo_path = clone_repo(payload.repo_url, payload.git_token)
            content_scan = scan_for_env_vars(repo_path)
            secret_keywords = ["JWT_SECRET", "SECRET_KEY", "SESSION_SECRET", "AUTH_TOKEN", "API_KEY", "ACCESS_TOKEN"]

            generated_count = 0
            for keyword in secret_keywords:
                if keyword in content_scan and keyword not in final_env_vars:
                    final_env_vars[keyword] = secrets.token_urlsafe(32)
                    yield json.dumps({"status": "healing", "message": f"🔑 Auto-generated missing secret: {keyword}"}) + "\n"
                    generated_count += 1

            if generated_count > 0:
                yield json.dumps({"status": "info", "message": f"Agent auto-configured {generated_count} security keys."}) + "\n"

        except Exception as e:
            yield json.dumps({"status": "warning", "message": f"Repo/Secret scan failed: {e}. Proceeding..."}) + "\n"

        yield json.dumps({"status": "starting", "message": f"Deploying {full_image_tag} with {len(final_env_vars)} config variables..."}) + "\n"

        for chunk in deploy_to_server(
            payload.public_ip,
            full_image_tag,
            final_env_vars,
            private_key_str=payload.private_key,
            docker_user=payload.docker_username,
            docker_token=payload.docker_token,
            repo_path=repo_path
        ):
            yield json.dumps(chunk) + "\n"

    return StreamingResponse(log_streamer(), media_type="application/x-ndjson")


@app.post("/api/v1/metrics")
def get_metrics(payload: MetricsRequest):
    try:
        session = boto3.Session(
            aws_access_key_id=payload.aws_access_key,
            aws_secret_access_key=payload.aws_secret_key,
            region_name=payload.region
        )
        ec2 = session.client('ec2')
        cloudwatch = session.client('cloudwatch')

        response = ec2.describe_instances(
            Filters=[{'Name': 'ip-address', 'Values': [payload.public_ip]}]
        )
        reservations = response.get('Reservations', [])
        if not reservations:
            return {"status": "error", "message": "Instance not found"}
        instance_id = reservations[0]['Instances'][0]['InstanceId']

        metrics_config = [
            ('CPUUtilization', 'Percent'),
            ('NetworkIn', 'Bytes'),
            ('NetworkOut', 'Bytes'),
            ('DiskReadBytes', 'Bytes'),
            ('DiskWriteBytes', 'Bytes')
        ]

        end_time = datetime.utcnow()
        start_time = end_time - timedelta(hours=1)
        results = {}

        for metric_name, unit in metrics_config:
            stats = cloudwatch.get_metric_statistics(
                Namespace='AWS/EC2',
                MetricName=metric_name,
                Dimensions=[{'Name': 'InstanceId', 'Value': instance_id}],
                StartTime=start_time,
                EndTime=end_time,
                Period=300,
                Statistics=['Average']
            )
            datapoints = sorted(stats['Datapoints'], key=lambda x: x['Timestamp'])
            results[metric_name] = [
                {"time": d['Timestamp'].strftime("%H:%M"), "value": round(d['Average'], 2)}
                for d in datapoints
            ]

        return {"status": "success", "instance_id": instance_id, "data": results}

    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/v1/runtime-logs")
def get_runtime_logs(payload: LogsRequest):
    client = None
    try:
        repo_name = payload.repo_url.split("/")[-1].replace(".git", "").lower()
        key_path = "/root/.ssh/id_rsa"
        if not os.path.exists(key_path):
            return {"status": "info", "logs": "Waiting for SSH keys to be established..."}

        key = paramiko.RSAKey.from_private_key_file(key_path)
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(hostname=payload.public_ip, username="ubuntu", pkey=key, timeout=5, banner_timeout=5)

        cmd = f"docker ps -q --filter ancestor={payload.docker_username}/{repo_name}:latest | xargs -r docker logs --tail {payload.lines}"
        stdin, stdout, stderr = client.exec_command(cmd)
        logs = stdout.read().decode('utf-8')
        error = stderr.read().decode('utf-8')

        if not logs and error:
            return {"status": "info", "logs": error}
        if not logs:
            return {"status": "info", "logs": "Container starting... No logs yet."}

        return {"status": "success", "logs": logs}

    except Exception as e:
        return {"status": "info", "logs": f"Connecting to server... ({str(e)})"}
    finally:
        if client: client.close()

# ============================================================
# SECTION 12: ROUTES — EXPORT, PUSH CONFIG, WEBHOOKS
# ============================================================

@app.post("/api/v1/export-config")
def export_config(payload: ExportRequest):
    try:
        repo_path = clone_repo(payload.repo_url, payload.git_token)
        zip_buffer = BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
            target_extensions = {'.tf', '.json', '.yml', '.yaml', '.toml', '.sh'}
            target_names = {'Dockerfile', 'docker-compose.yml', 'Makefile', 'Procfile'}

            found_files = False
            for root, dirs, files in os.walk(repo_path):
                dirs[:] = [d for d in dirs if d not in {'.git', 'node_modules', 'venv', '__pycache__', '.next'}]
                for file in files:
                    if file in target_names or os.path.splitext(file)[1] in target_extensions:
                        abs_path = os.path.join(root, file)
                        rel_path = os.path.relpath(abs_path, repo_path)
                        zip_file.write(abs_path, rel_path)
                        found_files = True

        zip_buffer.seek(0)

        if not found_files:
            return {"status": "error", "message": "No config files found. Did you run the build yet?"}

        filename = f"xiiops-eject-{uuid.uuid4().hex[:6]}.zip"
        return StreamingResponse(
            zip_buffer,
            media_type="application/zip",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )

    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/v1/push-config")
def push_config_to_github(payload: GitHubPushRequest):
    try:
        repo_path = clone_repo(payload.repo_url, payload.git_token)
        repo = git.Repo(repo_path)
        required_files = ['Dockerfile', 'main.tf']
        missing = [f for f in required_files if not os.path.exists(os.path.join(repo_path, f))]

        if missing:
            return {"status": "error", "message": f"Config files missing: {missing}. Run a build first."}

        branch_name = "xiiops-config"
        repo.git.reset('--hard')
        try:
            repo.git.checkout('main')
        except:
            repo.git.checkout('master')

        try:
            repo.git.checkout('-b', branch_name)
        except git.GitCommandError:
            repo.git.checkout(branch_name)

        files_to_commit = ['Dockerfile', 'main.tf', 'docker-compose.yml', 'Makefile']
        for f in files_to_commit:
            if os.path.exists(os.path.join(repo_path, f)):
                repo.index.add([f])

        try:
            repo.index.commit("chore(xiiops): export infrastructure configuration")
        except:
            pass

        clean_url = payload.repo_url.replace("https://", "").replace("http://", "")
        auth_url = f"https://{payload.github_token}@{clean_url}"
        repo.git.push(auth_url, f"{branch_name}:{branch_name}")

        return {"status": "success", "message": f"Pushed branch '{branch_name}' to GitHub.", "branch": branch_name}

    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/v1/webhook")
async def github_webhook(request: Request, background_tasks: BackgroundTasks):
    print("\n🔔 Webhook received!")
    try:
        payload = await request.json()

        if 'zen' in payload:
            return {"status": "success", "message": "Ping received"}

        if 'ref' not in payload or 'repository' not in payload:
            return {"status": "ignored", "message": "Not a push event"}

        repo_url = payload['repository']['clone_url']
        branch = payload['ref'].replace('refs/heads/', '')
        pusher = payload.get('pusher', {}).get('name', 'Unknown')
        message = payload.get('head_commit', {}).get('message', 'No commit message')

        if branch not in ['main', 'master']:
            return {"status": "ignored", "message": f"Push to {branch} ignored."}

        background_tasks.add_task(run_cicd_pipeline, repo_url, pusher, message)
        return {"status": "success", "message": "CI/CD Pipeline Triggered"}

    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/v1/pipeline-status")
def get_pipeline_status(payload: PipelineStatusRequest):
    requested_url = payload.repo_url.lower().strip()
    repo_name_request = requested_url.split("/")[-1].replace(".git", "")

    for key in PIPELINE_STATE.keys():
        if repo_name_request in key:
            return PIPELINE_STATE[key]

    return {"status": "idle", "logs": []}


@app.get("/api/v1/project/status")
def get_project_status(repo_url: str):
    registry_path = "project_registry.json"
    if os.path.exists(registry_path):
        try:
            with open(registry_path, "r") as f:
                registry = json.load(f)

            key = repo_url.lower().strip()
            if key in registry:
                return {"status": "found", "data": registry[key]}
            if key + ".git" in registry:
                return {"status": "found", "data": registry[key + ".git"]}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    return {"status": "not_found"}


if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
