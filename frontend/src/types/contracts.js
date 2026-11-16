/** @typedef {{id:number,code:string,weekly_reading:number,completion:number,night_share:number,note:string}} Reader */
/** @typedef {{a:string,b:string,basis:string}} MergeEdge */
/** @typedef {{canonical_code:string,source_codes:string[],size:number,merge_basis:MergeEdge[],weekly_reading:number|null,completion:number|null,night_share:number|null,zero_articles:boolean,quarantined:boolean,quarantine_reason:string|null,unknown_codes:string[]}} Identity */
/** @typedef {{id:number,name:string,state:string,relations:MergeEdge[],stats:object|null,identity_count:number|null,error:string,checksum:string,attempts:number}} IdentityBatch */
/** @typedef {{id:number,name:string,k:number,features:string[],state:string,result:object,identity_batch_id:number|null,identity_version:number,identity_checksum:string}} Segment */
/** @typedef {{kind:string,identity:string,source_codes:string[],clusters:number[],reason:string}} Conflict */
/** @typedef {{id:number,segment_id:number,cluster_id:number,title:string,note:string,members:string[],status:string,identity_version:number,identity_checksum:string,member_details:object[],conflicts:Conflict[],approval_identity_version:number|null,approved_at:string}} Outreach */
