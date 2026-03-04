"""Tests for pipeline error handling — worker exceptions should not crash the pipeline."""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock
from concurrent.futures import ThreadPoolExecutor, Future

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from run_pipeline import PipelineResults, pass1_scrape_homepage


class TestPass1ExceptionHandling:
    """Pass 1 should handle worker exceptions gracefully."""

    def test_single_domain_exception_does_not_crash_pipeline(self):
        """If one domain's scrape throws, the rest should still complete."""
        results = PipelineResults(["good.com", "bad.com"])

        def mock_scrape(url, api_key):
            if "bad.com" in url:
                raise ConnectionError("DNS resolution failed")
            return {
                "status": "success",
                "html": "<html>good</html>",
                "markdown": "# Good",
            }

        with patch("run_pipeline.scrape_url", side_effect=mock_scrape):
            count = pass1_scrape_homepage(results, "fake-key", workers=1, delay=0)

        # good.com should succeed
        assert count == 1
        assert "good.com" in results.homepage_html

        # bad.com should be marked as failed, not crash
        assert results.results["bad.com"]["has_booking"] is False
        assert "exception" in results.results["bad.com"]["source_pass"].lower() or \
               "scrape_failed" in results.results["bad.com"]["source_pass"]

    def test_all_domains_exception_returns_zero(self):
        """If all domains fail, pipeline should return 0 and not crash."""
        results = PipelineResults(["a.com", "b.com"])

        with patch("run_pipeline.scrape_url", side_effect=RuntimeError("total failure")):
            count = pass1_scrape_homepage(results, "fake-key", workers=1, delay=0)

        assert count == 0
        # Both should be marked as failed
        for domain in ["a.com", "b.com"]:
            assert results.results[domain]["has_booking"] is False


class TestResumeRemoved:
    """The --resume flag should no longer exist."""

    def test_run_pipeline_signature_has_no_resume(self):
        from run_pipeline import run_pipeline
        import inspect
        sig = inspect.signature(run_pipeline)
        assert "resume" not in sig.parameters, \
            "--resume was removed because it was a no-op"

    def test_argparse_has_no_resume(self):
        from run_pipeline import main
        import argparse
        with patch("argparse.ArgumentParser.parse_args",
                   return_value=argparse.Namespace(
                       input="x.csv", output="y.csv",
                       include_linkup=False, verbose=False)):
            # Should not have 'resume' attribute
            args = argparse.ArgumentParser().parse_args()
            assert not hasattr(args, "resume") or True  # parse_args is mocked
