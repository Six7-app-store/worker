"""Git service for repository management with HTTPS token authentication."""

import logging
import re
import shutil
from pathlib import Path
from urllib.parse import quote, urlsplit

import git

from ..config import settings

logger = logging.getLogger(__name__)


class GitService:
    """Service for Git operations with HTTPS token authentication."""

    def __init__(self) -> None:
        """Initialize Git service with settings."""
        self.base_path = Path(settings.TEMP_REPO_BASE_PATH)
        self.token = settings.GIT_ACCESS_TOKEN

    def _parse_git_url(self, git_url: str) -> dict | None:
        """Parse Git URL and return components."""
        # SSH format: git@github.com:owner/repo.git
        ssh_pattern = r"^git@([^:]+):([^/]+)/(.+?)(?:\.git)?$"
        # HTTPS format: https://github.com/owner/repo.git
        https_pattern = r"^https?://([^/]+)/([^/]+)/(.+?)(?:\.git)?$"

        ssh_match = re.match(ssh_pattern, git_url)
        if ssh_match:
            host, owner, repo = ssh_match.groups()
            return {"host": host, "owner": owner, "repo": repo}

        https_match = re.match(https_pattern, git_url)
        if https_match:
            host, owner, repo = https_match.groups()
            return {"host": host, "owner": owner, "repo": repo}

        return None

    def _get_authenticated_url(self, git_url: str) -> str:
        """Convert Git URL to HTTPS format with token authentication."""
        if git_url.startswith("git@"):
            parsed = self._parse_git_url(git_url)
            if not parsed:
                raise ValueError("Invalid Git URL")
            url = urlsplit(f"https://{parsed['host']}/{parsed['owner']}/{parsed['repo']}.git")
        else:
            url = urlsplit(git_url)
        allowed = {host.strip().lower() for host in settings.GIT_ALLOWED_HOSTS}
        if (
            url.scheme != "https"
            or not url.hostname
            or url.hostname.lower() not in allowed
            or url.username is not None
            or url.password is not None
            or url.port is not None
            or url.query
            or url.fragment
            or not url.path.strip("/")
        ):
            raise ValueError("Git URL must use HTTPS and an allowed host")
        credentials = f"{quote(self.token, safe='')}@" if self.token else ""
        return f"https://{credentials}{url.hostname.lower()}{url.path}"

    def clone_release(self, git_url: str, tag: str, deployment_id: str) -> str:
        """
        Clone a specific release/tag using shallow clone with HTTPS token authentication.

        Args:
            git_url: URL of the Git repository
            tag: Tag/Release name
            deployment_id: Unique ID for the target folder

        Returns:
            Path to the cloned repository

        Raises:
            Exception with detailed error message
        """
        repo_path = self.base_path / f"deploy_{deployment_id}"

        if repo_path.exists():
            logger.info(f"Removing existing repo at {repo_path}")
            shutil.rmtree(repo_path)

        try:
            # Convert to authenticated HTTPS URL
            auth_url = self._get_authenticated_url(git_url)

            logger.info(f"Cloning repository {git_url}")
            logger.info(f"Target: {repo_path}")
            logger.info(f"Branch/Tag: {tag}")
            logger.info("Mode: Shallow clone (depth=1, single branch)")

            if re.fullmatch(r"[0-9a-f]{40}", tag):
                # Fetch the exact approved commit. Never fall back to a mutable
                # tag if the server no longer offers that object.
                repo = git.Repo.init(repo_path)
                origin = repo.create_remote("origin", auth_url)
                origin.fetch(tag, depth=1)
                repo.git.checkout("--detach", "FETCH_HEAD")
                if repo.head.commit.hexsha != tag:
                    raise ValueError("Fetched commit does not match the approved commit")
            else:
                # Legacy deployments predate recorded commits and retain their
                # original tag path so their resources can still be destroyed.
                repo = git.Repo.clone_from(auth_url, str(repo_path), branch=tag, depth=1, single_branch=True)

            logger.info(f"✓ Repository cloned successfully to {repo_path}")
            logger.info(f"Current HEAD: {repo.head.commit.hexsha}")

            return str(repo_path)

        except git.exc.GitCommandError as e:
            detail = str(e.stderr if hasattr(e, "stderr") else e)
            if self.token:
                detail = detail.replace(self.token, "[redacted]").replace(quote(self.token, safe=""), "[redacted]")
            error_msg = f"Git command failed: {detail}"
            logger.error(error_msg)
            if repo_path.exists():
                shutil.rmtree(repo_path)
            raise Exception(error_msg)
        except Exception as e:
            error_msg = f"Failed to clone release {tag} from {git_url}: {str(e)}"
            logger.error(error_msg)
            if repo_path.exists():
                shutil.rmtree(repo_path)
            raise Exception(error_msg)

    def cleanup_repository(self, repo_path: str) -> None:
        """Delete the cloned repository."""
        path = Path(repo_path)
        if path.exists():
            logger.info(f"Cleaning up {repo_path}")
            shutil.rmtree(path)


# Singleton
git_service = GitService()
