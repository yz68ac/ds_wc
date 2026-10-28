"""Stopped-listener metadata probe: image XML schema, cache match and resource columns."""
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
from wxbridge.app.bot import GroupSession
from wxbridge.config import load_config
from wxbridge.app.runtime import instance_lock


def main():
    cfg = load_config(ROOT/"config.json")
    with ExitStack() as stack:
        for name in sorted({cfg["queue"]["file"]+".lock", str(Path(cfg["decrypted_dir"])/".listener.lock")}):
            stack.enter_context(instance_lock(name))
        bot = GroupSession(cfg)
        try:
            rel, table = bot.store.message_table(bot.chat_id)
            conn = bot.store._connect(rel)
            columns = [r["name"] for r in conn.execute(f"PRAGMA table_info([{table}])")]
            result = {"message_columns": columns, "images": [], "resource_tables": []}
            fields = "local_id,local_type,create_time,message_content"
            if "WCDB_CT_message_content" in columns:
                fields += ",WCDB_CT_message_content"
            rows = conn.execute(f"SELECT {fields} FROM [{table}] WHERE (local_type & 4294967295)=3 ORDER BY local_id DESC LIMIT 5").fetchall()
            conn.close()
            root = Path(bot.store.db_dir).parent
            bucket = root/"msg/attach"/hashlib.md5(bot.chat_id.encode()).hexdigest()
            result["target_bucket_exists"] = bucket.is_dir()
            md5_values = []
            for row in rows:
                data = dict(row)
                text = bot.store._decode_content(data["message_content"], data.get("WCDB_CT_message_content"))
                if not text.lstrip().startswith("<") and ":\n" in text:
                    text = text.split(":\n", 1)[1]
                item = {"local_id": data["local_id"], "raw_type": data["local_type"]}
                try:
                    tree = ET.fromstring(text)
                    image = next(n for n in tree.iter() if n.tag.rsplit("}", 1)[-1] == "img")
                    item["attribute_lengths"] = {k: len(v) for k,v in image.attrib.items()}
                    md5 = image.attrib.get("md5", "")
                    md5_values.append(md5)
                    item["has_md5"] = bool(md5)
                    item["matches"] = sum(1 for p in bucket.glob(f"*/Img/{md5}*.dat")) if md5 else 0
                except (ET.ParseError, StopIteration):
                    item["valid_image_xml"] = False
                result["images"].append(item)
            conn = bot.store._connect("hardlink/hardlink.db")
            result["hardlink_match_counts"] = []
            for md5 in md5_values:
                candidates = (md5, md5.encode(), bytes.fromhex(md5))
                matches = conn.execute("SELECT typeof(md5) AS md5_type,length(md5) AS md5_length,file_name,dir1,dir2 FROM image_hardlink_info_v4 WHERE md5 IN (?,?,?)", candidates).fetchall()
                result["hardlink_match_counts"].append({"matches": len(matches), "metadata": [dict(r) for r in matches[:3]]})
            sample = conn.execute("SELECT typeof(md5) AS md5_type,length(md5) AS md5_length,file_name,dir1,dir2 FROM image_hardlink_info_v4 LIMIT 3").fetchall()
            result["hardlink_samples"] = [dict(r) for r in sample]
            result["directory_lookup"] = [{"rowid": r[0], "chars": len(r[1]), "is_target": r[1] in (bot.chat_id, hashlib.md5(bot.chat_id.encode()).hexdigest()), "is_month": len(r[1]) == 7 and r[1][4:5] == "-"} for r in conn.execute("SELECT rowid,username FROM dir2id LIMIT 12")]
            conn.close()
            conn = bot.store._connect("message/message_resource.db")
            resource = conn.execute("SELECT i.message_local_id,i.packed_info AS info_blob,d.packed_info AS detail_blob,d.type,d.data_index FROM MessageResourceInfo i LEFT JOIN MessageResourceDetail d ON i.message_id=d.message_id WHERE i.chat_id IN (SELECT rowid FROM ChatName2Id WHERE user_name=?) AND (i.message_local_type & 4294967295)=3 ORDER BY i.message_local_id DESC LIMIT 5", (bot.chat_id,)).fetchall()
            import re
            result["resource_blob_metadata"] = []
            for row in resource:
                item = {"local_id": row["message_local_id"], "detail_type": row["type"], "data_index_type": type(row["data_index"]).__name__}
                for name in ("info_blob", "detail_blob"):
                    data = row[name] or b""
                    if isinstance(data, str):
                        data = data.encode()
                    item[name] = {"bytes": len(data), "hex32_count": len(re.findall(rb"[a-f0-9]{32}", data)), "paths_count": len(re.findall(rb"[A-Za-z0-9_/.-]+\\.dat", data))}
                result["resource_blob_metadata"].append(item)
            conn.close()
            for resource_db in ("message/message_resource.db", "hardlink/hardlink.db"):
                conn = bot.store._connect(resource_db)
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()[:12]:
                    name = row[0]
                    columns = [r["name"] for r in conn.execute('PRAGMA table_info("'+name.replace('"','""')+'")')]
                    result["resource_tables"].append({"db": resource_db, "table": name, "columns": columns})
                conn.close()
            print(json.dumps(result, ensure_ascii=False, indent=2))
        finally:
            bot.close()


if __name__ == "__main__":
    main()
