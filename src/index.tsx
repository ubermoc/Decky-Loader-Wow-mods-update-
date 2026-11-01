import { callable, definePlugin } from "@decky/api";
import { ButtonItem, Field, PanelSection, PanelSectionRow } from "@decky/ui";
import { useCallback, useEffect, useState } from "react";
import { FaCloudDownloadAlt } from "react-icons/fa";

interface WowInstall {
  flavor: string;
  label: string;
  root: string;
  addons_path: string;
  addon_count: number;
  addons_modified: number | null;
}

interface ScanResult {
  ok: boolean;
  error: string | null;
  curseforge: { app_id: number; name: string; exe: string } | null;
  installs: WowInstall[];
}

interface UpdateInfo {
  installed: { version: string; build: string };
  latest: { version: string | null; build: string | null } | null;
  update_available: boolean;
  error: string | null;
}

interface InstallResult {
  ok: boolean;
  error: string | null;
  installed: { version: string | null; build: string | null } | null;
}

const scan = callable<[], ScanResult>("scan");
const getUpdateInfo = callable<[], UpdateInfo>("get_update_info");
const installUpdate = callable<[], InstallResult>("install_update");

function describeBuild(version: string | null | undefined, build: string | null | undefined): string {
  if (!build) return version ?? "unknown";
  const short = build.slice(0, 7);
  return version && version !== "unknown" ? `${version} (${short})` : short;
}

function formatModified(epochSeconds: number | null): string {
  if (epochSeconds === null) return "never";
  return new Date(epochSeconds * 1000).toLocaleString();
}

function SetupInstructions({ onRecheck }: { onRecheck: () => void }) {
  return (
    <PanelSection title="Set up CurseForge">
      <PanelSectionRow>
        <Field focusable label="CurseForge isn't in your Steam library yet" bottomSeparator="none">
          <div style={{ whiteSpace: "normal", lineHeight: 1.4 }}>
            1. Switch to Desktop Mode.
            <br />
            2. Download the CurseForge app for Linux (AppImage) from curseforge.com/download/app and mark it
            executable.
            <br />
            3. In Steam choose Add a Game, then Add a Non-Steam Game, and select the AppImage.
            <br />
            4. Return to Gaming Mode and press Check again.
          </div>
        </Field>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem layout="below" onClick={onRecheck}>
          Check again
        </ButtonItem>
      </PanelSectionRow>
    </PanelSection>
  );
}

function InstallList({ installs }: { installs: WowInstall[] }) {
  if (installs.length === 0) {
    return (
      <PanelSection title="World of Warcraft">
        <PanelSectionRow>
          <Field focusable label="No WoW installs found" bottomSeparator="none">
            <div style={{ whiteSpace: "normal" }}>
              Launch WoW once from Battle.net in Gaming Mode so its Proton prefix exists, then check again.
            </div>
          </Field>
        </PanelSectionRow>
      </PanelSection>
    );
  }
  return (
    <PanelSection title="World of Warcraft">
      {installs.map((install) => (
        <PanelSectionRow key={install.addons_path}>
          <Field
            focusable
            label={install.label}
            description={`${install.addon_count} addons - AddOns changed ${formatModified(install.addons_modified)}`}
            bottomSeparator="none"
          />
        </PanelSectionRow>
      ))}
    </PanelSection>
  );
}

function UpdatesSection() {
  const [info, setInfo] = useState<UpdateInfo | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [installedNow, setInstalledNow] = useState(false);

  const check = useCallback(async () => {
    setBusy(true);
    try {
      setInfo(await getUpdateInfo());
    } catch (err) {
      setMessage(`Update check failed: ${String(err)}`);
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    void check();
  }, [check]);

  const install = useCallback(async () => {
    setBusy(true);
    setMessage(null);
    try {
      const result = await installUpdate();
      if (result.ok) {
        setInstalledNow(true);
        setMessage(
          "Update installed. Open Decky's plugin list (gear icon) and choose Reload for WoW Addon Updater to start using it.",
        );
      } else {
        setMessage(result.error ?? "Update failed.");
      }
    } catch (err) {
      setMessage(`Update failed: ${String(err)}`);
    } finally {
      setBusy(false);
    }
  }, []);

  const installed = info ? describeBuild(info.installed.version, info.installed.build) : "...";
  const latest = info?.latest ? describeBuild(info.latest.version, info.latest.build) : info ? "unavailable" : "...";
  const canInstall = !!info?.update_available && !busy && !installedNow;
  const status = installedNow
    ? "Installed - reload the plugin"
    : info?.error
      ? info.error
      : info?.update_available
        ? "Update available"
        : info
          ? "Up to date"
          : "Checking...";

  return (
    <PanelSection title="Updates">
      <PanelSectionRow>
        <Field focusable label="Installed version" bottomSeparator="none">
          {installed}
        </Field>
      </PanelSectionRow>
      <PanelSectionRow>
        <Field focusable label="Latest version" description={status} bottomSeparator="none">
          {latest}
        </Field>
      </PanelSectionRow>
      {message && (
        <PanelSectionRow>
          <Field focusable label={message} bottomSeparator="none" />
        </PanelSectionRow>
      )}
      <PanelSectionRow>
        <ButtonItem layout="below" disabled={!canInstall} onClick={install}>
          {busy ? "Working..." : "Install update"}
        </ButtonItem>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem layout="below" disabled={busy} onClick={check}>
          Check for updates
        </ButtonItem>
      </PanelSectionRow>
    </PanelSection>
  );
}

function MainContent() {
  const [result, setResult] = useState<ScanResult | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    setBusy(true);
    try {
      setResult(await scan());
    } catch (err) {
      setResult({ ok: false, error: String(err), curseforge: null, installs: [] });
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  if (result === null) {
    return (
      <PanelSection>
        <PanelSectionRow>
          <Field label="Scanning..." />
        </PanelSectionRow>
      </PanelSection>
    );
  }

  if (!result.ok) {
    return (
      <PanelSection title="Scan failed">
        <PanelSectionRow>
          <Field focusable label={result.error ?? "Unknown error"} />
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" disabled={busy} onClick={refresh}>
            Try again
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
    );
  }

  // Once the CurseForge shortcut exists the setup message never shows again.
  if (result.curseforge === null) {
    return <SetupInstructions onRecheck={refresh} />;
  }

  return (
    <>
      <InstallList installs={result.installs} />
      <PanelSection>
        <PanelSectionRow>
          <ButtonItem layout="below" disabled={busy} onClick={refresh}>
            Rescan
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
    </>
  );
}

function Content() {
  return (
    <>
      <MainContent />
      <UpdatesSection />
    </>
  );
}

export default definePlugin(() => ({
  name: "WoW Addon Updater",
  titleView: <div>WoW Addon Updater</div>,
  content: <Content />,
  icon: <FaCloudDownloadAlt />,
  onDismount() {},
}));
