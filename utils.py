import os
import shutil
import git
from urllib.parse import urlparse, urlunparse

def get_file_tree(root_dir: str):
    """Scans the directory and returns a string representation."""
    file_structure = []
    ignore_dirs = {'.git', 'node_modules', 'venv', '__pycache__', '.idea', '.vscode'}

    for root, dirs, files in os.walk(root_dir):
        dirs[:] = [d for d in dirs if d not in ignore_dirs]
        level = root.replace(root_dir, '').count(os.sep)
        indent = ' ' * 4 * level
        file_structure.append(f"{indent}{os.path.basename(root)}/")
        subindent = ' ' * 4 * (level + 1)
        for f in files:
            file_structure.append(f"{subindent}{f}")

    return "\n".join(file_structure)

def clone_repo(repo_url: str, git_token: str = None):
    """
    Clones the repo. If git_token is provided, injects it into the HTTPS URL.
    """
    repo_name = repo_url.split("/")[-1].replace(".git", "").lower()
    target_dir = os.path.join("/tmp/xiiops_repos", repo_name)

    if os.path.exists(target_dir):
        shutil.rmtree(target_dir)

    auth_url = repo_url
    if git_token and repo_url.startswith("https://"):
        parsed = urlparse(repo_url)
        new_netloc = f"{git_token}@{parsed.netloc}"
        auth_url = urlunparse((parsed.scheme, new_netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))

    print(f"⬇️ Cloning {repo_url} (Auth: {'Yes' if git_token else 'No'})...")

    try:
        git.Repo.clone_from(auth_url, target_dir)
        return target_dir
    except Exception as e:
        safe_error = str(e).replace(git_token, "***") if git_token else str(e)
        raise Exception(f"Git Clone Failed: {safe_error}")
