import { afterEach, describe, expect, it, vi } from "vitest";
import {
  EDITOR_IMAGE_MAX_BYTES,
  EDITOR_IMAGE_MAX_PIXELS,
  classifyEditorImageUploadFailure,
  fitEditorImageDimensions,
  prepareEditorImage,
} from "@/lib/image-assets";

describe("editor image preparation", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("fits high-resolution images within both server decode boundaries", () => {
    const fitted = fitEditorImageDimensions(4_000, 4_000);

    expect(fitted.width).toBeLessThan(4_000);
    expect(fitted.height).toBeLessThan(4_000);
    expect(fitted.width * fitted.height).toBeLessThanOrEqual(EDITOR_IMAGE_MAX_PIXELS);
    expect(fitEditorImageDimensions(4_000, 3_000)).toEqual({
      width: 4_000,
      height: 3_000,
    });
    expect(fitEditorImageDimensions(10_000, 1_000)).toEqual({
      width: 8_192,
      height: 819,
    });
  });

  it("optimizes a sub-1 MB image whose decoded resolution exceeds 12 MP", async () => {
    const original = new File(
      [new Uint8Array(750 * 1024)],
      "camera.jpg",
      { type: "image/jpeg", lastModified: 123 },
    );
    const close = vi.fn();
    const drawImage = vi.fn();
    vi.stubGlobal("createImageBitmap", vi.fn().mockResolvedValue({
      width: 4_000,
      height: 4_000,
      close,
    }));
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({
      drawImage,
    } as unknown as CanvasRenderingContext2D);
    vi.spyOn(HTMLCanvasElement.prototype, "toBlob").mockImplementation(
      (callback, type) => callback(new Blob([new Uint8Array(600 * 1024)], { type: type || "" })),
    );

    const prepared = await prepareEditorImage(original);

    expect(original.size).toBeLessThan(1024 * 1024);
    expect(prepared.optimized).toBe(true);
    expect(prepared.file).not.toBe(original);
    expect(prepared.file.name).toBe("camera.jpg");
    expect(prepared.file.type).toBe("image/jpeg");
    expect(prepared.file.size).toBeLessThan(EDITOR_IMAGE_MAX_BYTES);
    expect((prepared.width || 0) * (prepared.height || 0)).toBeLessThanOrEqual(
      EDITOR_IMAGE_MAX_PIXELS,
    );
    expect(drawImage).toHaveBeenCalledWith(
      expect.objectContaining({ width: 4_000, height: 4_000 }),
      0,
      0,
      prepared.width,
      prepared.height,
    );
    expect(close).toHaveBeenCalledOnce();
  });

  it("does not silently flatten an oversized animated image", async () => {
    const close = vi.fn();
    vi.stubGlobal("createImageBitmap", vi.fn().mockResolvedValue({
      width: 4_000,
      height: 4_000,
      close,
    }));

    await expect(
      prepareEditorImage(new File(["gif"], "motion.gif", { type: "image/gif" })),
    ).rejects.toThrow(/preserve its animation/);
    expect(close).toHaveBeenCalledOnce();
  });
});

describe("editor image upload error recovery", () => {
  it("marks size and format failures as non-retryable with a recovery path", () => {
    const tooLarge = classifyEditorImageUploadFailure(
      Object.assign(new Error("Image dimensions are too large"), { status: 413 }),
      new File(["image"], "phone.jpg", { type: "image/jpeg" }),
    );
    const invalid = classifyEditorImageUploadFailure(
      Object.assign(new Error("Invalid encoding"), { status: 415 }),
    );

    expect(tooLarge).toEqual({
      message: expect.stringMatching(/phone\.jpg.*10 MB, 12 MP.*8,192.*Choose another/),
      retryable: false,
    });
    expect(invalid.retryable).toBe(false);
    expect(invalid.message).toMatch(/PNG, JPEG, GIF, or WebP/);
  });

  it("keeps transient server failures retryable", () => {
    expect(
      classifyEditorImageUploadFailure(
        Object.assign(new Error("Storage temporarily unavailable"), { status: 503 }),
      ),
    ).toEqual({
      message: "the server could not store the image right now. Try again.",
      retryable: true,
    });
  });
});
