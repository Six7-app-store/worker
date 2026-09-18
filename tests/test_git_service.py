"""Tests for Git service."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import git
import pytest

from app.services.git_service import GitService


@pytest.fixture
def git_service(tmp_path):
    """Create GitService instance with temporary base path."""
    with patch("app.services.git_service.settings") as mock_settings:
        mock_settings.TEMP_REPO_BASE_PATH = str(tmp_path)
        mock_settings.GIT_ACCESS_TOKEN = "test-token-123"
        service = GitService()
        return service


class TestGitServiceURLParsing:
    """Test URL parsing functionality."""

    def test_parse_ssh_url(self, git_service):
        """Test parsing SSH Git URL."""
        url = "git@github.com:owner/repo.git"
        result = git_service._parse_git_url(url)

        assert result is not None
        assert result["host"] == "github.com"
        assert result["owner"] == "owner"
        assert result["repo"] == "repo"

    def test_parse_https_url(self, git_service):
        """Test parsing HTTPS Git URL."""
        url = "https://github.com/owner/repo.git"
        result = git_service._parse_git_url(url)

        assert result is not None
        assert result["host"] == "github.com"
        assert result["owner"] == "owner"
        assert result["repo"] == "repo"

    def test_parse_url_without_git_extension(self, git_service):
        """Test parsing URL without .git extension."""
        url = "https://gitlab.com/group/project"
        result = git_service._parse_git_url(url)

        assert result is not None
        assert result["host"] == "gitlab.com"
        assert result["owner"] == "group"
        assert result["repo"] == "project"

    def test_parse_invalid_url(self, git_service):
        """Test parsing invalid URL returns None."""
        url = "not-a-valid-git-url"
        result = git_service._parse_git_url(url)

        assert result is None


class TestGitServiceAuthentication:
    """Test authentication URL generation."""

    def test_convert_ssh_to_authenticated_https(self, git_service):
        """Test converting SSH URL to authenticated HTTPS."""
        ssh_url = "git@github.com:owner/repo.git"
        result = git_service._get_authenticated_url(ssh_url)

        assert result.startswith("https://test-token-123@")
        assert "github.com/owner/repo.git" in result

    def test_add_token_to_https_url(self, git_service):
        """Test adding token to HTTPS URL."""
        https_url = "https://github.com/owner/repo.git"
        result = git_service._get_authenticated_url(https_url)

        assert "test-token-123@github.com" in result

    def test_authenticated_url_format(self, git_service):
        """Test authenticated URL has correct format."""
        url = "git@gitlab.com:mygroup/myrepo.git"
        result = git_service._get_authenticated_url(url)

        assert result.startswith("https://")
        assert "@gitlab.com" in result


class TestGitServiceCloning:
    """Test repository cloning functionality."""

    @patch("app.services.git_service.git.Repo.clone_from")
    def test_clone_release_success(self, mock_clone, git_service, tmp_path):
        """Test successful repository cloning."""
        mock_repo = MagicMock()
        mock_repo.head.commit.hexsha = "abc123def456"
        mock_clone.return_value = mock_repo

        git_url = "https://github.com/owner/repo.git"
        tag = "v1.0.0"
        deployment_id = "test-deploy-123"

        result = git_service.clone_release(git_url, tag, deployment_id)

        assert result is not None
        assert "deploy_test-deploy-123" in result
        mock_clone.assert_called_once()

        # Verify clone parameters
        call_args = mock_clone.call_args
        assert call_args.kwargs["branch"] == tag
        assert call_args.kwargs["depth"] == 1
        assert call_args.kwargs["single_branch"] is True

    @patch("app.services.git_service.git.Repo.clone_from")
    def test_clone_release_removes_existing_directory(self, mock_clone, git_service, tmp_path):
        """Test that existing directory is removed before cloning."""
        mock_repo = MagicMock()
        mock_repo.head.commit.hexsha = "abc123"
        mock_clone.return_value = mock_repo

        deployment_id = "test-deploy-456"
        existing_path = tmp_path / f"deploy_{deployment_id}"
        existing_path.mkdir(parents=True)

        # Verify directory exists before cloning
        assert existing_path.exists()

        git_service.clone_release("https://github.com/test/repo.git", "v1.0.0", deployment_id)

        # Should have been removed and recreated
        mock_clone.assert_called_once()

    @patch("app.services.git_service.git.Repo.clone_from")
    def test_clone_release_failure_cleanup(self, mock_clone, git_service, tmp_path):
        """Test cleanup on clone failure."""
        mock_clone.side_effect = Exception("Clone failed")

        with pytest.raises(Exception, match="Failed to clone"):
            git_service.clone_release("https://github.com/test/repo.git", "v1.0.0", "test-fail")

        # Verify directory was cleaned up
        fail_path = tmp_path / "deploy_test-fail"
        assert not fail_path.exists()


class TestGitServiceCleanup:
    """Test repository cleanup functionality."""

    def test_cleanup_existing_directory(self, git_service, tmp_path):
        """Test cleanup of existing directory."""
        test_dir = tmp_path / "test_cleanup"
        test_dir.mkdir(parents=True)
        (test_dir / "test_file.txt").write_text("test content")

        assert test_dir.exists()

        git_service.cleanup_repository(str(test_dir))

        assert not test_dir.exists()

    def test_cleanup_nonexistent_directory(self, git_service, tmp_path):
        """Test cleanup handles nonexistent directory gracefully."""
        nonexistent = tmp_path / "does_not_exist"

        # Should not raise an exception
        git_service.cleanup_repository(str(nonexistent))

        assert not nonexistent.exists()


class TestGitServiceCloneFailureCleanup:
    """Die Aufraeum-Zweige in beiden except-Handlern.

    ``clone_release`` entfernt ein vorhandenes Ziel *vor* dem Klonen. Ein
    Verzeichnis, das beim Fehlschlag existiert, kann also nur der Klon
    selbst angelegt haben - git laesst einen halben Checkout stehen, wenn
    es mitten im Transfer stirbt. Ohne das rmtree startet der naechste
    Deploy derselben ID auf diesem Schutt.
    """

    @patch("app.services.git_service.git.Repo.clone_from")
    def test_partial_checkout_is_removed_after_a_git_error(self, mock_clone, git_service, tmp_path):
        """GitCommandError-Pfad: Schutt weg, Meldung durchgereicht."""
        target = tmp_path / "deploy_git-err"

        def _leave_debris(*_args, **_kwargs):
            (target / ".git").mkdir(parents=True, exist_ok=True)
            raise git.exc.GitCommandError("clone", 128, stderr="remote hung up")

        mock_clone.side_effect = _leave_debris

        with pytest.raises(Exception, match="Git command failed"):
            git_service.clone_release("https://github.com/test/repo.git", "v1.0.0", "git-err")

        assert not target.exists()

    @patch("app.services.git_service.git.Repo.clone_from")
    def test_partial_checkout_is_removed_after_an_unexpected_error(self, mock_clone, git_service, tmp_path):
        """Derselbe Schutz im generischen Handler, nicht nur bei Git-Fehlern."""
        target = tmp_path / "deploy_other-err"

        def _leave_debris(*_args, **_kwargs):
            target.mkdir(parents=True, exist_ok=True)
            raise MemoryError("out of memory mid-clone")

        mock_clone.side_effect = _leave_debris

        with pytest.raises(Exception, match="Failed to clone"):
            git_service.clone_release("https://github.com/test/repo.git", "v1.0.0", "other-err")

        assert not target.exists()


@pytest.fixture
def source_repo(tmp_path):
    """Ein echtes lokales Git-Repository: zwei Commits, Tag auf dem ersten.

    Der zweite Commit ist der Punkt der Uebung - nur so zeigt sich, ob
    ``clone_release`` wirklich den Tag auscheckt und nicht einfach HEAD.
    """
    src = tmp_path / "source"
    src.mkdir()
    repo = git.Repo.init(src)
    with repo.config_writer() as cw:
        cw.set_value("user", "name", "Test")
        cw.set_value("user", "email", "test@example.invalid")

    (src / "version.txt").write_text("v1\n")
    repo.index.add(["version.txt"])
    repo.index.commit("erste Fassung")
    repo.create_tag("v1.0.0")

    (src / "version.txt").write_text("spaeter\n")
    repo.index.add(["version.txt"])
    repo.index.commit("danach")

    return src


@pytest.mark.integration
class TestGitServiceIntegration:
    """Integrationstests gegen ein echtes Git-Repository.

    Kein Netzzugriff: das Gegenstueck ist ein lokal angelegtes Repository,
    das ueber eine ``file://``-URL geklont wird. Die URL-Form ist nicht
    beliebig - bei einem reinen Pfad-Klon ignoriert git ``--depth``, der
    Shallow-Clone waere dann ungeprueft. ``_get_authenticated_url`` laesst
    ``file://`` unangetastet, weil es weder mit ``git@`` noch mit
    ``https://`` beginnt.

    Diese Lane lief zuvor leer: sie enthielt einen einzigen, mit
    ``skip`` markierten Platzhalter, wodurch der Coverage-Wert des Laufs
    auf 0 fiel und das Gate den Job kippte.
    """

    def test_clone_release_checks_out_the_tag_not_head(self, git_service, source_repo, tmp_path):
        """Der Tag zeigt auf den ersten Commit, HEAD auf den zweiten."""
        path = git_service.clone_release(source_repo.as_uri(), "v1.0.0", "int-tag")

        assert Path(path) == tmp_path / "deploy_int-tag"
        assert (Path(path) / "version.txt").read_text() == "v1\n"

    def test_clone_release_is_shallow(self, git_service, source_repo):
        """depth=1: im Klon darf genau ein Commit liegen."""
        path = git_service.clone_release(source_repo.as_uri(), "v1.0.0", "int-shallow")

        assert len(list(git.Repo(path).iter_commits())) == 1

    def test_clone_release_replaces_an_existing_checkout(self, git_service, source_repo):
        """Ein zweiter Lauf raeumt den alten Stand weg, statt ihn zu mischen."""
        first = git_service.clone_release(source_repo.as_uri(), "v1.0.0", "int-again")
        (Path(first) / "stale.txt").write_text("alt\n")

        second = git_service.clone_release(source_repo.as_uri(), "v1.0.0", "int-again")

        assert first == second
        assert not (Path(second) / "stale.txt").exists()

    def test_clone_release_raises_and_cleans_up_for_unknown_tag(self, git_service, source_repo, tmp_path):
        """Ein fehlender Tag darf kein halbes Verzeichnis hinterlassen."""
        with pytest.raises(Exception, match="Git command failed"):
            git_service.clone_release(source_repo.as_uri(), "v9.9.9", "int-missing")

        assert not (tmp_path / "deploy_int-missing").exists()
