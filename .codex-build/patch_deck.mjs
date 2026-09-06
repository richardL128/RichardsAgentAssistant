import fs from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { FileBlob, PresentationFile } from "@oai/artifact-tool";

const WORKSPACE = "/Users/richardliu/Desktop/LifeAgent";
const SOURCE = path.join(WORKSPACE, "presentations/LifeAgent-Intern-Architecture-v2.pptx");
const FINAL = path.join(WORKSPACE, "presentations/LifeAgent-Intern-Architecture.pptx");
const BUILD = path.join(WORKSPACE, ".codex-build/lifeagent-deck-finalizer");
const SKILL_DIR = "/Users/richardliu/.codex/plugins/cache/openai-primary-runtime/presentations/26.903.11726/skills/presentations";
const RUNTIME_PYTHON = "/Users/richardliu/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3.12";

const presentation = await PresentationFile.importPptx(await FileBlob.load(SOURCE));

const topologySubtitle = presentation.resolve("sh/cza94vmx");
topologySubtitle.text = "One image runs the API or one of three queue-specific workers.";
topologySubtitle.position = { left: 55, top: 92, width: 285, height: 72 };
topologySubtitle.text.fontSize = 16;
topologySubtitle.text.autoFit = "shrinkText";

const durableCoreBody = presentation.resolve("sh/2xkrih8b");
durableCoreBody.text = "Postgres is the recovery boundary. External side effects become durable intents before any message is sent.";
durableCoreBody.position = { left: 184, top: 621, width: 882, height: 21 };
durableCoreBody.text.fontSize = 13;
durableCoreBody.text.autoFit = "shrinkText";

const flowSlides = [
  { index: 4, y: 312.5, color: "#1AA6B2" },
  { index: 5, y: 302.5, color: "#D98A00" },
  { index: 6, y: 302.5, color: "#2D5F88" },
];
for (const flow of flowSlides) {
  const slide = presentation.slides.getItem(flow.index);
  for (const shape of [...slide.shapes.items]) {
    if (shape.connector) shape.delete();
  }
  for (const x of [220, 426, 632, 838, 1044]) {
    slide.shapes.add({
      geometry: "line",
      position: { left: x, top: flow.y, width: 28, height: 0 },
      fill: "none",
      line: { style: "solid", fill: flow.color, width: 1.5 },
    });
  }
}

await fs.mkdir(BUILD, { recursive: true });
const candidatePath = path.join(BUILD, "candidate.pptx");
await (await PresentationFile.exportPptx(presentation)).save(candidatePath);

const { finalizePresentation } = await import(pathToFileURL(path.join(SKILL_DIR, "container_tools/artifact_tool_utils.mjs")).href);
const result = await finalizePresentation({
  explicitTotalSlideCount: 12,
  requiredNativeTableOwnerSlides: [],
  requiredNativeChartOwnerSlides: [],
  requiredEmbeddedWorkbookChartOwnerSlides: [],
  workspaceDir: WORKSPACE,
  candidatePath,
  finalPath: FINAL,
  pythonExecutable: RUNTIME_PYTHON,
  integrityValidatorPath: path.join(SKILL_DIR, "container_tools/inspect_presentation_package_integrity.py"),
  layoutValidatorPath: path.join(SKILL_DIR, "container_tools/inspect_presentation_layout_geometry.py"),
  layoutArgs: [
    "--expected-slide-size-emu", "12192000,6858000",
    "--validate-bullet-geometry",
    "--validate-heading-fit",
  ],
  fontPolicy: {
    basis: "reference",
    families: ["Aptos"],
    referencePath: SOURCE,
    referenceSha256: "168979949679f450b27ee456f5a07f119870194ff64ce2a71e3b4ff581b84f67",
  },
  verifyArtifactToolImport: true,
  receiptPath: path.join(BUILD, "LifeAgent-Intern-Architecture.validation.json"),
});

console.log(JSON.stringify(result, null, 2));
