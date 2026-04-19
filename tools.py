import os
import subprocess
from github import Github, Auth
from dotenv import load_dotenv
from langchain_core.tools import tool

load_dotenv()

# --- FIX: Graceful GitHub Auth ---
github_token = os.getenv("GITHUB_TOKEN", "").strip()

# FIX: Strictly validate the token format so placeholders don't crash the script
if github_token and (github_token.startswith("ghp_") or github_token.startswith("github_pat_")):
    auth = Auth.Token(github_token)
    gh_client = Github(auth=auth)
else:
    # Fallback to unauthenticated access for public repos
    gh_client = Github()

@tool
def analyze_github_repo(repo_url: str) -> str:
    """
    Scans a GitHub repository, returns the directory structure,
    and extracts the contents of key dependency files to understand the tech stack.
    """
    try:
        repo_path = repo_url.replace("https://github.com/", "").replace(".git", "")
        if repo_path.endswith("/"):
            repo_path = repo_path[:-1]

        repo = gh_client.get_repo(repo_path)
        contents = repo.get_contents("")

        file_tree = []
        critical_files = {}
        target_files = ["package.json", "requirements.txt", "Dockerfile", "docker-compose.yml", "pom.xml"]

        for content_file in contents:
            file_tree.append(content_file.name)
            if content_file.name in target_files:
                file_data = repo.get_contents(content_file.path)
                critical_files[content_file.name] = file_data.decoded_content.decode("utf-8")

        analysis = f"Repository: {repo_path}\n"
        analysis += f"Root Files: {', '.join(file_tree)}\n\n"

        if critical_files:
            analysis += "Critical File Contents:\n"
            for name, data in critical_files.items():
                analysis += f"\n--- {name} ---\n{data[:2000]}\n"
        else:
            analysis += "No standard dependency files found in the root."

        return analysis

    except Exception as e:
        return f"Error accessing repository {repo_url}: {str(e)}"


@tool
def save_infrastructure_code(file_name: str, code_content: str) -> str:
    """
    Saves generated Infrastructure-as-Code (Terraform/OpenTofu) or CI/CD files to the local disk.
    Always use this tool when the user asks you to generate and save infrastructure or deployment files.
    """
    save_dir = "generated_iac"
    if not os.path.exists(save_dir):
        os.makedirs(save_dir)

    safe_file_name = os.path.basename(file_name)
    file_path = os.path.join(save_dir, safe_file_name)

    try:
        with open(file_path, "w") as f:
            clean_content = code_content.replace("```terraform", "").replace("```hcl", "").replace("```", "").strip()
            f.write(clean_content)
        return f"Success! I have generated the code and saved it to {save_dir}/{safe_file_name}."
    except Exception as e:
        return f"Critical Error: Failed to save file. Details: {str(e)}"


@tool
def execute_terraform_plan(directory: str = "generated_iac") -> str:
    """
    Executes 'terraform init' and 'terraform plan' in the specified directory.
    Use this tool to validate the generated Terraform code and show the user what AWS resources will be provisioned.
    """
    try:
        init_process = subprocess.run(
            ["terraform", "init"],
            cwd=directory,
            capture_output=True,
            text=True
        )
        if init_process.returncode != 0:
            return f"Terraform Init Failed. Fix the syntax errors:\n{init_process.stderr}"

        plan_process = subprocess.run(
            ["terraform", "plan", "-no-color"],
            cwd=directory,
            capture_output=True,
            text=True
        )

        if plan_process.returncode != 0:
            return f"Terraform Plan Failed. Fix the errors:\n{plan_process.stderr}"

        return f"Terraform Plan Successful. Here is the output:\n{plan_process.stdout[:4000]}"

    except FileNotFoundError:
        return "Critical Error: Terraform CLI is not installed on the host system."
    except Exception as e:
        return f"Error executing Terraform: {str(e)}"


@tool
def read_local_file(file_name: str, directory: str = "generated_iac") -> str:
    """
    Reads the content of a local file from the disk.
    Always use this tool to read existing configuration or Terraform files before trying to modify or fix them.
    """
    safe_file_name = os.path.basename(file_name)
    file_path = os.path.join(directory, safe_file_name)

    try:
        with open(file_path, "r") as f:
            return f.read()
    except FileNotFoundError:
        return f"Error: The file {safe_file_name} was not found in the {directory} directory."
    except Exception as e:
        return f"Error reading file. Details: {str(e)}"


@tool
def save_cicd_workflow(file_name: str, code_content: str) -> str:
    """
    Saves generated CI/CD workflow files (like GitHub Actions .yml files) to the local disk.
    Always use this tool when the user asks you to generate deployment pipelines, CI/CD workflows, or GitHub Actions.
    """
    save_dir = ".github/workflows"
    if not os.path.exists(save_dir):
        os.makedirs(save_dir)

    safe_file_name = os.path.basename(file_name)
    if not safe_file_name.endswith(('.yml', '.yaml')):
        safe_file_name += '.yml'

    file_path = os.path.join(save_dir, safe_file_name)

    try:
        with open(file_path, "w") as f:
            clean_content = code_content.replace("```yaml", "").replace("```yml", "").replace("```", "").strip()
            f.write(clean_content)
        return f"Success! CI/CD workflow generated and saved to {file_path}."
    except Exception as e:
        return f"Critical Error: Failed to save CI/CD file. Details: {str(e)}"
