export const JEV_MODEL = 'jev-latest';
export const DEFAULT_SURE = 0.85;
export const DEFAULT_PRICE_PER_MILLION_INPUT = 0.042;

const TIER_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}(:[^\s:]{1,128})?$/;

export interface JevSettings {
  enabled: boolean;
  sure: number;
  tiers: Record<string, string>;
  pricePerMillionInput: number;
}

export const JEV_OFF: JevSettings = Object.freeze({
  enabled: false,
  sure: DEFAULT_SURE,
  tiers: Object.freeze({}) as Record<string, string>,
  pricePerMillionInput: DEFAULT_PRICE_PER_MILLION_INPUT,
});

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function tiersOf(value: unknown): Record<string, string> {
  if (value === undefined || value === null) return {};
  if (!isObject(value)) throw new Error('jev.tiers must be an object of tier name to description');
  return Object.fromEntries(
    Object.entries(value).map(([name, text]) => {
      if (!TIER_NAME.test(name)) throw new Error(`jev.tiers name '${name}' must be <profile> or <profile>:<model>`);
      if (typeof text !== 'string' || text.trim() === '') throw new Error(`jev.tiers.${name} must be a description`);
      return [name, text];
    }),
  );
}

export function parseJevSettings(value: unknown): JevSettings {
  if (value === undefined || value === null) return JEV_OFF;
  if (!isObject(value)) throw new Error('jev must be an object');
  const { enabled, sure, tiers, pricePerMillionInput } = value;
  if (enabled !== undefined && typeof enabled !== 'boolean') throw new Error('jev.enabled must be true or false');
  if (sure !== undefined && (typeof sure !== 'number' || !(sure > 0 && sure <= 1))) {
    throw new Error('jev.sure must be a number above 0 and at most 1');
  }
  if (
    pricePerMillionInput !== undefined &&
    (typeof pricePerMillionInput !== 'number' || !Number.isFinite(pricePerMillionInput) || pricePerMillionInput < 0)
  ) {
    throw new Error('jev.pricePerMillionInput must be a number of dollars, 0 or more');
  }
  return {
    enabled: enabled === true,
    sure: (sure as number | undefined) ?? DEFAULT_SURE,
    tiers: tiersOf(tiers),
    pricePerMillionInput: (pricePerMillionInput as number | undefined) ?? DEFAULT_PRICE_PER_MILLION_INPUT,
  };
}
