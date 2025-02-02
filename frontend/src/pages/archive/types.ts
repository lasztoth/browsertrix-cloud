type CrawlState =
  | "starting"
  | "running"
  | "complete"
  | "failed"
  | "partial_complete"
  | "timed_out"
  | "stopping"
  | "canceled";

export type Crawl = {
  id: string;
  userid: string;
  userName: string;
  aid: string;
  cid: string;
  configName: string;
  schedule: string;
  manual: boolean;
  started: string; // UTC ISO date
  finished?: string; // UTC ISO date
  state: CrawlState;
  scale: number;
  stats: { done: string; found: string } | null;
  resources?: { name: string; path: string; hash: string; size: number }[];
  fileCount?: number;
  fileSize?: number;
  completions?: number;
};

type SeedConfig = {
  scopeType?: string;
  limit?: number;
  extraHops?: number;
};

export type CrawlConfig = {
  seeds: (string | ({ url: string } & SeedConfig))[];
} & SeedConfig;

export type CrawlTemplate = {
  id: string;
  name: string;
  schedule: string;
  userid: string;
  userName: string | null;
  created: string;
  crawlCount: number;
  lastCrawlId: string;
  lastCrawlTime: string;
  lastCrawlState: CrawlState;
  currCrawlId: string;
  newId: string | null;
  oldId: string | null;
  inactive: boolean;
  config: CrawlConfig;
  scale: number;
  profileid: string | null;
  profileName: string | null;
};

export type Profile = {
  id: string;
  name: string;
  description: string;
  created: string;
  origins: string[];
  profileId: string;
  baseProfileName: string;
  aid: string;
  crawlconfigs: { id: string; name: string }[];
};
