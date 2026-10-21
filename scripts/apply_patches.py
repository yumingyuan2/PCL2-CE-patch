#!/usr/bin/env python3
"""Apply the PCL2-CE-patch changes to an upstream PCL-CE source checkout.

The patches are deliberately explicit: if upstream refactors one of the
anchors below, this script fails the workflow instead of silently publishing
an unpatched build.

Usage:
    python3 scripts/apply_patches.py <upstream-source-dir> --repo owner/name
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

MARKER = "PCL2-CE-patch"
SKIP_DIRS = {".git", "bin", "obj", ".vs"}


class PatchError(RuntimeError):
    pass


def read_source(path: Path) -> tuple[str, str]:
    """Return (text, newline) while preserving the BOM and line endings."""
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig")
    newline = "\r\n" if "\r\n" in text else "\n"
    return text, newline


def write_source(path: Path, text: str, had_bom: bool) -> None:
    data = text.encode("utf-8")
    if had_bom:
        data = b"\xef\xbb\xbf" + data
    path.write_bytes(data)


def read_plain(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def find_source(root: Path, filename: str, needle: str | None = None) -> Path:
    matches = [
        path
        for path in root.rglob(filename)
        if not any(part in SKIP_DIRS for part in path.parts)
    ]
    if needle is not None:
        matches = [path for path in matches if needle in read_plain(path)]
    if not matches:
        raise PatchError(f"Could not find {filename!r} in {root}")
    if len(matches) > 1:
        raise PatchError(
            f"Found multiple candidates for {filename!r}: "
            + ", ".join(str(path.relative_to(root)) for path in matches)
        )
    return matches[0]


def sub_once(
    text: str,
    pattern: str,
    replacement: str,
    description: str,
    *,
    flags: int = re.MULTILINE,
) -> tuple[str, bool]:
    new_text, count = re.subn(pattern, replacement, text, count=1, flags=flags)
    if count != 1:
        raise PatchError(f"Patch anchor not found: {description}")
    return new_text, True


def patch_right_hint(root: Path) -> None:
    path = find_source(root, "PageLaunchRight.xaml.cs", "PanHint")
    text, newline = read_source(path)
    marker = "// PCL2-CE-patch: hide the CE notice on the right"

    if marker not in text:
        # The notice is a MyCard named PanHint.  Its visibility is the only
        # thing that needs to be forced to Collapsed.
        expression = (
            r"(?P<indent>[ \t]*)PanHint\.Visibility\s*=\s*States\.Hint\.CEMessage\s*\r?\n"
            r"[ \t]*\?\s*Visibility\.Visible\s*\r?\n"
            r"[ \t]*:\s*Visibility\.Collapsed\s*;"
        )
        replacement = (
            r"\g<indent>PanHint.Visibility = Visibility.Collapsed; "
            "// PCL2-CE-patch: hide the CE notice on the right"
        )
        text, _ = sub_once(text, expression, replacement, "PageLaunchRight.PanHint.Visibility")

    write_source(path, text, path.read_bytes().startswith(b"\xef\xbb\xbf"))

    # Also flip the persisted default, so a fresh profile never enables it.
    # This is secondary to the forced Collapsed above and is therefore
    # allowed to be absent in a future refactor.
    try:
        states_path = find_source(root, "States.cs", "UiLauncherCEHint")
        states_text, _ = read_source(states_path)
        states_pattern = r'(\[ConfigItem<bool>\("UiLauncherCEHint",\s*)true(\s*\)\])'
        states_new, count = re.subn(states_pattern, r"\1false\2", states_text, count=1)
        if count == 1:
            write_source(states_path, states_new, states_path.read_bytes().startswith(b"\xef\xbb\xbf"))
            print(f"  patched {states_path.relative_to(root)} (default=false)")
        else:
            print("  note: UiLauncherCEHint default was already changed; skipped")
    except PatchError:
        print("  note: UiLauncherCEHint default not found; the forced Collapsed patch is still active")

    print(f"  patched {path.relative_to(root)} (right-side CE notice hidden)")


def patch_region_policy(root: Path) -> None:
    region_path = find_source(root, "RegionPolicy.cs", "RegionAssessment")
    text, _ = read_source(region_path)
    marker = "public bool IsRestricted => false; // PCL2-CE-patch"
    central_patched = marker in text

    if not central_patched:
        pattern = (
            r"public\s+bool\s+IsRestricted\s*=>\s*Classification\s+is\s+"
            r"RegionClassification\.Mainland\s+or\s+RegionClassification\.Unknown\s*;"
        )
        replacement = f"{marker}: allow Minecraft downloads in every region"
        text, central_patched = sub_once(
            text,
            pattern,
            replacement,
            "RegionAssessment.IsRestricted",
        )

    write_source(region_path, text, region_path.read_bytes().startswith(b"\xef\xbb\xbf"))

    policy_is_restricted_patched = False
    check_patched = False
    try:
        policy_path = find_source(root, "RestrictedFeaturePolicy.cs", "RestrictedFeaturePolicy")
    except PatchError:
        policy_path = None

    if policy_path is not None:
        policy_text, _ = read_source(policy_path)
        policy_marker = "internal static bool IsRestricted => false; // PCL2-CE-patch"
        policy_is_restricted_patched = policy_marker in policy_text

        if not policy_is_restricted_patched:
            pattern = r"internal\s+static\s+bool\s+IsRestricted\s*=>\s*Assess\(\)\.IsRestricted\s*;"
            try:
                policy_text, policy_is_restricted_patched = sub_once(
                    policy_text,
                    pattern,
                    policy_marker,
                    "RestrictedFeaturePolicy.IsRestricted",
                )
            except PatchError:
                policy_is_restricted_patched = False

        # Fallback for a future refactor that removes RegionAssessment.IsRestricted
        # but keeps the central Check helper.  The current code does not need this.
        check_patched = "// PCL2-CE-patch: region gate disabled" in policy_text
        if not central_patched and not check_patched:
            pattern = (
                r"(internal\s+static\s+bool\s+Check\(string feature, bool showHint = true\)\s*\{\s*\r?\n)"
                r"([ \t]*)var assessment = Assess\(\);\s*\r?\n"
                r"[ \t]*if \(!assessment\.IsRestricted\) return true;"
            )
            replacement = (
                r"\1\2return true; // PCL2-CE-patch: region gate disabled"
            )
            try:
                policy_text, check_patched = sub_once(
                    policy_text,
                    pattern,
                    replacement,
                    "RestrictedFeaturePolicy.Check",
                )
            except PatchError:
                check_patched = False

        write_source(policy_path, policy_text, policy_path.read_bytes().startswith(b"\xef\xbb\xbf"))

    if not (central_patched or (policy_is_restricted_patched and check_patched)):
        raise PatchError(
            "Could not disable the regional feature gate. "
            "Neither RegionAssessment.IsRestricted nor the RestrictedFeaturePolicy fallbacks matched."
        )

    print(f"  patched {region_path.relative_to(root)} (region gate disabled)")


UPDATER_SOURCE = r'''using System.Text.Json.Nodes;
using PCL.Core.App.Localization;
using PCL.Core.Utils;
using PCL.Network;
using PCL.Network.Loaders;

namespace PCL;

/// <summary>
/// Reads releases from the PCL2-CE-patch repository.  Only assets ending in
/// "_patched.exe" are considered, so an accidentally uploaded upstream binary
/// can never be offered as an update.
/// </summary>
public class UpdatesPclCePatchModel : IUpdateSource
{
    private const string ReleasesApi =
        "https://api.github.com/repos/__REPOSITORY__/releases?per_page=100";
    private const string PatchedSuffix = "-patched";

    private JsonArray? _releases;

    public string SourceName { get; set; } = "PCL2-CE-Patch";

    public bool IsAvailable() => true;

    public bool RefreshCache()
    {
        var json = Requester.FetchJson(ReleasesApi, new RequestParam
        {
            Accept = "application/vnd.github+json",
            UseBrowserUserAgent = true,
            Timeout = 30000,
            Retries = 2
        });
        _releases = json as JsonArray
                    ?? throw new Exception("Patched update source returned an invalid response.");
        return true;
    }

    public VersionDataModel GetLatestVersion(UpdateChannel channel, UpdateArch arch)
    {
        var release = FindRelease(channel, arch, out var asset);
        return new VersionDataModel
        {
            VersionName = GetVersionName(release),
            VersionCode = 0,
            Sha256 = GetSha256(asset),
            Source = SourceName,
            Changelog = release["body"]?.ToString() ?? ""
        };
    }

    public bool IsLatest(UpdateChannel channel, UpdateArch arch, SemVer currentVersion, int currentVersionCode)
    {
        var latest = GetLatestVersion(channel, arch);
        return currentVersion >= SemVer.Parse(latest.VersionName);
    }

    public VersionAnnouncementDataModel GetAnnouncementList()
    {
        // Announcements remain an upstream-only feature.  Returning an empty
        // list keeps AnnouncementService happy without re-enabling upstream
        // version downloads.
        return new VersionAnnouncementDataModel
        {
            Content = new List<VersionAnnouncementContentModel>()
        };
    }

    public List<ModLoader.LoaderBase> GetDownloadLoader(UpdateChannel channel, UpdateArch arch, string output)
    {
        var release = FindRelease(channel, arch, out var asset);
        var url = asset["browser_download_url"]?.ToString()
                  ?? throw new Exception("Patched release asset has no download URL.");

        var loaders = new List<ModLoader.LoaderBase>();
        loaders.Add(new ModLoader.LoaderTask<int, List<DownloadFile>>(
            Lang.Text("Update.Task.GetDownloadInfo"), load =>
            {
                load.output = new List<DownloadFile> { new(new[] { url }, output) };
            }));
        loaders.Add(new LoaderDownload(Lang.Text("Update.Task.DownloadUpdateFile"), new List<DownloadFile>()));
        return loaders;
    }

    private JsonObject FindRelease(UpdateChannel channel, UpdateArch arch, out JsonObject asset)
    {
        if (_releases is null)
            RefreshCache();

        var wantPrerelease = channel == UpdateChannel.beta;
        JsonObject? selected = null;
        JsonObject? selectedAsset = null;
        SemVer? selectedVersion = null;

        foreach (var node in _releases!)
        {
            if (node is not JsonObject release)
                continue;
            if ((bool?)release["draft"] ?? false)
                continue;
            if (((bool?)release["prerelease"] ?? false) != wantPrerelease)
                continue;

            var tag = release["tag_name"]?.ToString() ?? "";
            if (!tag.StartsWith('v') ||
                !tag.EndsWith(PatchedSuffix, StringComparison.OrdinalIgnoreCase))
                continue;

            var version = tag[1..^PatchedSuffix.Length];
            if (!SemVer.TryParse(version, out var parsedVersion))
                continue;

            var found = FindAsset(release, channel, arch);
            if (found is null)
                continue;

            // Pick the highest version instead of trusting API ordering.  This
            // keeps updates correct even if an older release is rebuilt later.
            if (selectedVersion is not null && parsedVersion <= selectedVersion)
                continue;

            selected = release;
            selectedAsset = found;
            selectedVersion = parsedVersion;
        }

        if (selected is not null && selectedAsset is not null)
        {
            asset = selectedAsset;
            return selected;
        }

        throw new Exception(
            $"No patched {(wantPrerelease ? "beta" : "stable")} release with a {arch} asset was found.");
    }

    private static JsonObject? FindAsset(JsonObject release, UpdateChannel channel, UpdateArch arch)
    {
        var configuration = channel == UpdateChannel.beta ? "Beta" : "Release";
        var architecture = arch == UpdateArch.arm64 ? "ARM64" : "x64";
        var expected = $"PCL2_CE_{configuration}_{architecture}_patched.exe";

        if (release["assets"] is not JsonArray assets)
            return null;

        return assets.OfType<JsonObject>().FirstOrDefault(asset =>
            string.Equals(asset["name"]?.ToString(), expected, StringComparison.OrdinalIgnoreCase));
    }

    private static string GetVersionName(JsonObject release)
    {
        var tag = release["tag_name"]!.ToString();
        return tag[1..^PatchedSuffix.Length];
    }

    private static string GetSha256(JsonObject asset)
    {
        var digest = asset["digest"]?.ToString();
        if (string.IsNullOrWhiteSpace(digest))
            throw new Exception("Patched release asset has no sha256 digest.");

        if (digest.StartsWith("sha256:", StringComparison.OrdinalIgnoreCase))
            digest = digest[7..];
        if (digest.Length != 64)
            throw new Exception("Patched release asset has an invalid sha256 digest.");

        return digest.ToLowerInvariant();
    }
}
'''


def patch_first_profile_microsoft(root: Path) -> None:
    path = find_source(root, "ProfileUi.cs", "CanCreateOtherProfile")
    text, _ = read_source(path)
    marker = "return ProfileService.HasMicrosoftProfile; // PCL2-CE-patch"

    primary_ok = marker in text
    if not primary_ok:
        expression = (
            r"return\s+ProfileService\.HasMicrosoftProfile\s*\|\|\s*"
            r"\(Lang\.IsFeaturesUnrestricted\s*&&\s*ProfileService\.Profiles\.Count\s*>\s*0\)\s*\|\|\s*"
            r"NetworkHelper\.IsNetworkAvailable\(\)\s+is\s+false\s*;"
        )
        try:
            text, primary_ok = sub_once(
                text,
                expression,
                f"{marker}: the first profile must be a Microsoft account",
                "ProfileUi.CanCreateOtherProfile",
            )
        except PatchError:
            primary_ok = False

    if not primary_ok:
        # Fallback: force the only caller to pass includeOthers = false until a
        # Microsoft profile exists.  This keeps the behaviour even if the
        # CanCreateOtherProfile expression is refactored.
        fallback = r"(var\s+includeOthers\s*=\s*)CanCreateOtherProfile\(\)\s*;"
        try:
            text, fallback_ok = sub_once(
                text,
                fallback,
                r"\1ProfileService.HasMicrosoftProfile; // PCL2-CE-patch",
                "ProfileUi.CreateProfile includeOthers",
            )
        except PatchError:
            fallback_ok = False
        if not fallback_ok:
            raise PatchError("Could not enforce a Microsoft account for the first profile")

    write_source(path, text, path.read_bytes().startswith(b"\xef\xbb\xbf"))
    print(f"  patched {path.relative_to(root)} (first profile requires Microsoft)")


def patch_updater(root: Path, repository: str) -> None:
    updater_path = find_source(root, "UpdateManager.cs", "remoteServer")
    text, newline = read_source(updater_path)

    start = text.find("public static UpdatesWrapperModel remoteServer")
    if start < 0:
        raise PatchError("Could not find UpdateManager.remoteServer")

    list_start = text.find("new(new List<IUpdateSource>", start)
    if list_start < 0:
        raise PatchError("Could not find the remoteServer source list")

    brace_start = text.find("{", list_start)
    if brace_start < 0:
        raise PatchError("Could not find the start of the remoteServer source list")

    depth = 0
    close = -1
    for index in range(brace_start, len(text)):
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                close = index
                break
    if close < 0:
        raise PatchError("Could not find the end of the remoteServer source list")

    semicolon = text.find(";", close)
    if semicolon < 0:
        raise PatchError("Could not find the end of the remoteServer declaration")

    replacement = newline.join(
        [
            "public static UpdatesWrapperModel remoteServer = new(new List<IUpdateSource>",
            "    {",
            "        // PCL2-CE-patch: only use patched builds from this repository.",
            "        new UpdatesPclCePatchModel()",
            "    });",
        ]
    )
    text = text[:start] + replacement + text[semicolon + 1 :]
    write_source(updater_path, text, updater_path.read_bytes().startswith(b"\xef\xbb\xbf"))

    class_text = UPDATER_SOURCE.replace("__REPOSITORY__", repository)
    class_path = updater_path.with_name("UpdatesPclCePatchModel.cs")
    class_path.write_text(class_text, encoding="utf-8", newline="\n")

    print(f"  patched {updater_path.relative_to(root)} (updater points at {repository})")
    print(f"  added   {class_path.relative_to(root)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Path to the upstream PCL-CE checkout")
    parser.add_argument(
        "--repo",
        required=True,
        help="GitHub repository that publishes the patched releases, e.g. owner/PCL2-CE-patch",
    )
    args = parser.parse_args()

    root = args.source.resolve()
    if not (root / "Plain Craft Launcher 2.slnx").exists():
        print(f"error: {root} does not look like a PCL-CE checkout", file=sys.stderr)
        return 2

    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repo):
        print(f"error: invalid repository name: {args.repo}", file=sys.stderr)
        return 2

    print(f"Applying PCL2-CE patches to {root} ...")
    try:
        patch_right_hint(root)
        patch_region_policy(root)
        patch_first_profile_microsoft(root)
        patch_updater(root, args.repo)
    except PatchError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    print("All patches applied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
