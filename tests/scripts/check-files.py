#!/usr/bin/env python3

# This file is part of Mbed TLS (https://tls.mbed.org)
# Copyright (c) 2018, Arm Limited, All Rights Reserved

"""
This script checks the current state of the source code for minor issues,
including incorrect file permissions, presence of tabs, non-Unix line endings,
trailing whitespace, and presence of UTF-8 BOM.
Note: requires python 3, must be run from Mbed TLS root.
"""

import os
import argparse
import logging
import codecs
import sys


class FileIssueTracker:
    """Base class for file-wide issue tracking.

    To implement a checker that processes a file as a whole, inherit from
    this class and implement `check_file_for_issue` and define ``heading``.

    ``files_exemptions``: files whose name ends with a string in this set
     will not be checked.

    ``heading``: human-readable description of the issue
    """

    files_exemptions = frozenset()
    # heading must be defined in derived classes.
    # pylint: disable=no-member

    def __init__(self):
        self.files_with_issues = {}

    def should_check_file(self, filepath):
        """Whether the given file name should be checked.

        Files whose name ends with a string listed in ``self.files_exemptions``
        will not be checked.
        """
        for files_exemption in self.files_exemptions:
            if filepath.endswith(files_exemption):
                return False
        return True

    def check_file_for_issue(self, filepath):
        """Check the specified file for the issue that this class is for.

        Subclasses must implement this method.
        """
        raise NotImplementedError

    def record_issue(self, filepath, line_number):
        """Record that an issue was found at the specified location."""
        if filepath not in self.files_with_issues.keys():
            self.files_with_issues[filepath] = []
        self.files_with_issues[filepath].append(line_number)

    def output_file_issues(self, logger):
        """Log all the locations where the issue was found."""
        if self.files_with_issues.values():
            logger.info(self.heading)
            for filename, lines in sorted(self.files_with_issues.items()):
                if lines:
                    logger.info("{}: {}".format(
                        filename, ", ".join(str(x) for x in lines)
                    ))
                else:
                    logger.info(filename)
            logger.info("")

class LineIssueTracker(FileIssueTracker):
    """Base class for line-by-line issue tracking.

    To implement a checker that processes files line by line, inherit from
    this class and implement `line_with_issue`.
    """

    def issue_with_line(self, line, filepath):
        """Check the specified line for the issue that this class is for.

        Subclasses must implement this method.
        """
        raise NotImplementedError

    def check_file_line(self, filepath, line, line_number):
        if self.issue_with_line(line, filepath):
            self.record_issue(filepath, line_number)

    def check_file_for_issue(self, filepath):
        """Check the lines of the specified file.

        Subclasses must implement the ``issue_with_line`` method.
        """
        with open(filepath, "rb") as f:
            for i, line in enumerate(iter(f.readline, b"")):
                self.check_file_line(filepath, line, i + 1)


def is_windows_file(filepath):
    _root, ext = os.path.splitext(filepath)
    return ext in ('.bat', '.dsp', '.sln', '.vcxproj')


class PermissionIssueTracker(FileIssueTracker):
    """Track files with bad permissions.

    Files that are not executable scripts must not be executable."""

    heading = "Incorrect permissions:"

    def check_file_for_issue(self, filepath):
        is_executable = os.access(filepath, os.X_OK)
        should_be_executable = filepath.endswith((".sh", ".pl", ".py"))
        if is_executable != should_be_executable:
            self.files_with_issues[filepath] = None


class EndOfFileNewlineIssueTracker(FileIssueTracker):
    """Track files that end with an incomplete line
    (no newline character at the end of the last line)."""

    heading = "Missing newline at end of file:"

    def check_file_for_issue(self, filepath):
        with open(filepath, "rb") as f:
            if not f.read().endswith(b"\n"):
                self.files_with_issues[filepath] = None


class Utf8BomIssueTracker(FileIssueTracker):
    """Track files that start with a UTF-8 BOM.
    Files should be ASCII or UTF-8. Valid UTF-8 does not start with a BOM."""

    heading = "UTF-8 BOM present:"

    files_exemptions = frozenset([".vcxproj", ".sln"])

    def check_file_for_issue(self, filepath):
        with open(filepath, "rb") as f:
            if f.read().startswith(codecs.BOM_UTF8):
                self.files_with_issues[filepath] = None


class UnixLineEndingIssueTracker(LineIssueTracker):
    """Track files with non-Unix line endings (i.e. files with CR)."""

    heading = "Non-Unix line endings:"

    def should_check_file(self, filepath):
        return not is_windows_file(filepath)

    def issue_with_line(self, line, _filepath):
        return b"\r" in line


class WindowsLineEndingIssueTracker(LineIssueTracker):
    """Track files with non-Windows line endings (i.e. CR or LF not in CRLF)."""

    heading = "Non-Windows line endings:"

    def should_check_file(self, filepath):
        return is_windows_file(filepath)

    def issue_with_line(self, line, _filepath):
        return not line.endswith(b"\r\n") or b"\r" in line[:-2]


class TrailingWhitespaceIssueTracker(LineIssueTracker):
    """Track lines with trailing whitespace."""

    heading = "Trailing whitespace:"
    files_exemptions = frozenset([".dsp", ".md"])

    def issue_with_line(self, line, _filepath):
        return line.rstrip(b"\r\n") != line.rstrip()


class TabIssueTracker(LineIssueTracker):
    """Track lines with tabs."""

    heading = "Tabs present:"
    files_exemptions = frozenset([
        ".sln",
        "/Makefile",
        "/generate_visualc_files.pl",
    ])

    def issue_with_line(self, line, _filepath):
        return b"\t" in line


class MergeArtifactIssueTracker(LineIssueTracker):
    """Track lines with merge artifacts.
    These are leftovers from a ``git merge`` that wasn't fully edited."""

    heading = "Merge artifact:"

    def issue_with_line(self, line, _filepath):
        # Detect leftover git conflict markers.
        if line.startswith(b'<<<<<<< ') or line.startswith(b'>>>>>>> '):
            return True
        if line.startswith(b'||||||| '): # from merge.conflictStyle=diff3
            return True
        if line.rstrip(b'\r\n') == b'=======' and \
           not _filepath.endswith('.md'):
            return True
        return False


class IntegrityChecker:
    """Sanity-check files under the current directory."""

    def __init__(self, log_file):
        """Instantiate the sanity checker.
        Check files under the current directory.
        Write a report of issues to log_file."""
        self.check_repo_path()
        self.logger = None
        self.setup_logger(log_file)
        self.extensions_to_check = (
            ".bat",
            ".c",
            ".data",
            ".dsp",
            ".function",
            ".h",
            ".md",
            ".pl",
            ".py",
            ".sh",
            ".sln",
            ".vcxproj",
            "/CMakeLists.txt",
            "/ChangeLog",
            "/Makefile",
        )
        self.excluded_directories = ['.git', 'mbed-os', 'tinycrypt']
        self.excluded_paths = list(map(os.path.normpath, [
            'cov-int',
            'examples',
        ]))
        self.issues_to_check = [
            PermissionIssueTracker(),
            EndOfFileNewlineIssueTracker(),
            Utf8BomIssueTracker(),
            UnixLineEndingIssueTracker(),
            WindowsLineEndingIssueTracker(),
            TrailingWhitespaceIssueTracker(),
            TabIssueTracker(),
            MergeArtifactIssueTracker(),
        ]

    @staticmethod
    def check_repo_path():
        if not all(os.path.isdir(d) for d in ["include", "library", "tests"]):
            raise Exception("Must be run from Mbed TLS root")

    def setup_logger(self, log_file, level=logging.INFO):
        self.logger = logging.getLogger()
        self.logger.setLevel(level)
        if log_file:
            handler = logging.FileHandler(log_file)
            self.logger.addHandler(handler)
        else:
            console = logging.StreamHandler()
            self.logger.addHandler(console)

    def prune_branch(self, root, d):
        if d in self.excluded_directories:
            return True
        if os.path.normpath(os.path.join(root, d)) in self.excluded_paths:
            return True
        return False

    def check_files(self):
        for root, dirs, files in os.walk("."):
            dirs[:] = sorted(d for d in dirs if not self.prune_branch(root, d))
            for filename in sorted(files):
                filepath = os.path.join(root, filename)
                if not filepath.endswith(self.extensions_to_check):
                    continue
                for issue_to_check in self.issues_to_check:
                    if issue_to_check.should_check_file(filepath):
                        issue_to_check.check_file_for_issue(filepath)

    def output_issues(self):
        integrity_return_code = 0
        for issue_to_check in self.issues_to_check:
            if issue_to_check.files_with_issues:
                integrity_return_code = 1
            issue_to_check.output_file_issues(self.logger)
        return integrity_return_code


def run_main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-l", "--log_file", type=str, help="path to optional output log",
    )
    check_args = parser.parse_args()
    integrity_check = IntegrityChecker(check_args.log_file)
    integrity_check.check_files()
    return_code = integrity_check.output_issues()
    sys.exit(return_code)


if __name__ == "__main__":
    run_main()
