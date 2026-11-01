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

const scan = callable<[], ScanResult>("scan");

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

function Content() {
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

export default definePlugin(() => ({
  name: "WoW Addon Updater",
  titleView: <div>WoW Addon Updater</div>,
  content: <Content />,
  icon: <FaCloudDownloadAlt />,
  onDismount() {},
}));
