--
-- 002: 身份归并（同人多编号）
-- 可重复执行：表、列、索引均使用 IF NOT EXISTS。
-- 已有数据库在升级时手工执行本文件；应用启动只创建全新库的表。
--

CREATE TABLE IF NOT EXISTS public.identity_batches (
    id integer NOT NULL,
    name character varying(100),
    state character varying(20),
    relations json,
    snapshot json,
    result json,
    error text,
    checksum character varying(64),
    attempts integer,
    created character varying(40),
    created_by character varying(100)
);

CREATE SEQUENCE IF NOT EXISTS public.identity_batches_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.identity_batches_id_seq OWNED BY public.identity_batches.id;
ALTER TABLE ONLY public.identity_batches ALTER COLUMN id SET DEFAULT nextval('public.identity_batches_id_seq'::regclass);
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'identity_batches_pkey'
    ) THEN
        ALTER TABLE ONLY public.identity_batches ADD CONSTRAINT identity_batches_pkey PRIMARY KEY (id);
    END IF;
END
$$;

ALTER TABLE public.segments
    ADD COLUMN IF NOT EXISTS identity_batch_id integer,
    ADD COLUMN IF NOT EXISTS identity_version integer DEFAULT 0,
    ADD COLUMN IF NOT EXISTS identity_checksum character varying(64) DEFAULT '';

ALTER TABLE public.outreaches
    ADD COLUMN IF NOT EXISTS identity_batch_id integer,
    ADD COLUMN IF NOT EXISTS identity_version integer DEFAULT 0,
    ADD COLUMN IF NOT EXISTS identity_checksum character varying(64) DEFAULT '',
    ADD COLUMN IF NOT EXISTS member_details json,
    ADD COLUMN IF NOT EXISTS conflicts json,
    ADD COLUMN IF NOT EXISTS approval_identity_batch_id integer,
    ADD COLUMN IF NOT EXISTS approval_identity_version integer,
    ADD COLUMN IF NOT EXISTS approval_identity_checksum character varying(64) DEFAULT '',
    ADD COLUMN IF NOT EXISTS approved_at character varying(40) DEFAULT '';

CREATE INDEX IF NOT EXISTS identity_batches_state_idx ON public.identity_batches (state, id DESC);
