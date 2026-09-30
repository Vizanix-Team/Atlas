export interface TopEntry { id: string; symbol: string; value: number; venues: number }

export interface Overview {
  generated_at: string;
  generation_id: string;
  snapshot_effective_time: string;
  published_at: string | null;
  fixture?: boolean;
  counts: { assets: number; instruments: number; venues_contributing: number };
  qualified_assets: { multi_venue: number; single_venue: number };
  reported_volume_24h_usd: { total: number | null; spot: number | null; perpetual: number | null; note: string };
  median_dispersion_bps_multi_venue: number | null;
  top_reported_volume: TopEntry[];
  top_price_dispersion: TopEntry[];
  top_venue_count: TopEntry[];
  system: { venues_by_status: Record<string, string[]>; history_note: string };
}

export interface AssetRow {
  id: string;
  symbol: string;
  name: string | null;
  resolution: string;
  price: number | null;
  price_method: string | null;
  venues: number;
  volume_usd: number | null;
  spot_volume_usd: number | null;
  perp_volume_usd: number | null;
  dispersion_bps: number | null;
  change_24h: number | null;
  funding_8h: number | null;
  oi_usd: number | null;
  basis_bps: number | null;
  spread_bps: number | null;
  coverage: number | null;
  partial: boolean | null;
  file: string | null;
}

export interface VenueAssetRow {
  venue_slug: string;
  price_usd: number | null;
  price_source: string | null;
  deviation_bps: number | null;
  reference_weight: number | null;
  spread_bps: number | null;
  reported_volume_24h_usd: number | null;
  volume_share: number | null;
  funding_rate_8h: number | null;
  open_interest_usd: number | null;
  included_in_reference_price: boolean | null;
  exclusion_reason: string | null;
  conversion__rate: number | null;
  conversion__method: string | null;
}

export interface AssetDetail {
  generation_id: string;
  state: Record<string, unknown>;
  venues: VenueAssetRow[];
}

export interface VenueHealth {
  venue_slug: string; display_name: string; status: string; duration_ms: number;
  instrument_count: number; ticker_count: number; derivative_observation_count: number;
  order_book_count: number; parse_failures: number; http_errors: number; timeouts: number;
  rate_limit_responses: number; terms_url: string | null; attribution: string | null;
}
export interface VenuesDoc {
  venues: VenueHealth[];
  disabled: { venue_slug: string; display_name: string; reason: string | null }[];
}

export interface DatasetDoc {
  generation_id: string; schema_version: string; methodology_version: string;
  software_version: string; dataset_format_version: string; published_at: string | null;
  total_bytes: number;
  files: { filename: string; size_bytes: number; sha256: string; rows: number | null; table: string | null }[];
}

const cache = new Map<string, Promise<unknown>>();

export function load<T>(path: string): Promise<T> {
  let pending = cache.get(path);
  if (!pending) {
    pending = fetch(`data/${path}`, { cache: "no-cache" }).then((r) => {
      if (!r.ok) throw new Error(`Could not load ${path} (HTTP ${r.status})`);
      return r.json();
    });
    cache.set(path, pending);
  }
  return pending as Promise<T>;
}

export const loadOverview = () => load<Overview>("market-overview.json");
export const loadAssets = () => load<{ generation_id: string; assets: AssetRow[] }>("assets.json");
export const loadVenues = () => load<VenuesDoc>("venues.json");
export const loadDataset = () => load<DatasetDoc>("dataset.json");
export const loadAssetDetail = (file: string) => load<AssetDetail>(`asset/${file}`);
