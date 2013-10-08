#!/usr/bin/env python
import signal

# When running a subprocess, we may inherit the signal handler - remove it
signal.signal(signal.SIGINT, signal.SIG_DFL)

import sys, os, datetime, time, copy, json, traceback, csv, string
import multiprocessing, multiprocessing.queues, subprocess, re, ctypes
from optparse import OptionParser

try:
    import rethinkdb as r
except ImportError:
    print "The RethinkDB python driver is required to use this command."
    print "Please install the driver via `pip install rethinkdb`."
    exit(1)

info = "'rethinkdb export` exports data from a RethinkDB cluster into a directory"
usage = "\
  rethinkdb export [-c HOST:PORT] [-a AUTH_KEY] [-d DIR] [-e (DB | DB.TABLE)]...\n\
      [--format (csv | json)] [--fields FIELD,FIELD...]"

def print_export_help():
    print info
    print usage
    print ""
    print "  -h [ --help ]                    print this help"
    print "  -c [ --connect ] HOST:PORT       host and client port of a rethinkdb node to connect"
    print "                                   to (defaults to localhost:28015)"
    print "  -a [ --auth ] AUTH_KEY           authorization key for rethinkdb clients"
    print "  -d [ --directory ] DIR           directory to output to (defaults to"
    print "                                   rethinkdb_export_DATE_TIME)"
    print "  --format (csv | json)            format to write (defaults to json)"
    print "  --fields FIELD,FIELD...          limit the exported fields to those specified"
    print "                                   (required for CSV format)"
    print "  -e [ --export ] (DB | DB.TABLE)  limit dump to the given database or table (may"
    print "                                   be specified multiple times)"
    print ""
    print "EXAMPLES:"
    print "rethinkdb export -c mnemosyne:39500"
    print "  Export all data from a cluster running on host 'mnemosyne' with a client port at 39500."
    print ""
    print "rethinkdb export -e test -d rdb_export"
    print "  Export only the 'test' database on a local cluster into a named directory."
    print ""
    print "rethinkdb export -c hades -e test.subscribers -a hunter2"
    print "  Export a specific table from a cluster running on host 'hades' which requires authorization."
    print ""
    print "rethinkdb export --format csv -e test.history --fields time,message"
    print "  Export a specific table from a local cluster in CSV format with the fields 'time' and 'message'."
    print ""
    print "rethinkdb export --fields id,value -e test.data"
    print "  Export a specific table from a local cluster in JSON format with only the fields 'id' and 'value'."

def parse_options():
    parser = OptionParser(add_help_option=False, usage=usage)
    parser.add_option("-c", "--connect", dest="host", metavar="HOST:PORT", default="localhost:28015", type="string")
    parser.add_option("-a", "--auth", dest="auth_key", metavar="AUTHKEY", default="", type="string")
    parser.add_option("--format", dest="format", metavar="json | csv", default="json", type="string")
    parser.add_option("-d", "--directory", dest="directory", metavar="DIRECTORY", default=None, type="string")
    parser.add_option("-e", "--export", dest="tables", metavar="DB | DB.TABLE", default=[], action="append", type="string")
    parser.add_option("--fields", dest="fields", metavar="<FIELD>,<FIELD>...", default=None, type="string")
    parser.add_option("-h", "--help", dest="help", default=False, action="store_true")
    (options, args) = parser.parse_args()

    # Check validity of arguments
    if len(args) != 0:
        raise RuntimeError("Error: No positional arguments supported. Unrecognized option '%s'" % args[0])

    if options.help:
        print_export_help()
        exit(0)

    res = { }

    # Verify valid host:port --connect option
    host_port = options.host.split(":")
    if len(host_port) == 1:
        host_port = (host_port[0], "28015") # If just a host, use the default port
    if len(host_port) != 2:
        raise RuntimeError("Error: Invalid 'host:port' format: %s" % options.host)
    (res["host"], res["port"]) = host_port

    # Verify valid --format option
    if options.format not in ["csv", "json"]:
        raise RuntimeError("Error: Unknown format '%s', valid options are 'csv' and 'json'" % options.format)
    res["format"] = options.format

    # Verify valid directory option
    if options.directory is None:
        dirname = "./rethinkdb_export_%s" % datetime.datetime.today().strftime("%Y-%m-%dT%H:%M:%S")
    else:
        dirname = options.directory
    res["directory"] = os.path.abspath(dirname)

    if os.path.exists(res["directory"]):
        raise RuntimeError("Error: Output directory already exists: %s" % res["directory"])

    # Verify valid --export options
    res["tables"] = []
    for item in options.tables:
        if not all(c in string.ascii_letters + string.digits + "._" for c in item):
            raise RuntimeError("Error: Invalid 'db' or 'db.table' name: %s" % item)
        db_table = item.split(".")
        if len(db_table) == 1:
            res["tables"].append(db_table)
        elif len(db_table) == 2:
            res["tables"].append(tuple(db_table))
        else:
            raise RuntimeError("Error: Invalid 'db' or 'db.table' format: %s" % item)

    # Parse fields
    if options.fields is None:
        if options.format == "csv":
            raise RuntimeError("Error: Cannot write a CSV with no fields selected.  The '--fields' option must be specified.")
        res["fields"] = None
    elif len(res["tables"]) != 1 or len(res["tables"][0]) != 2:
        raise RuntimeError("Error: Can only use the --fields option when exporting a single table")
    else:
        res["fields"] = options.fields.split(",")

    res["auth_key"] = options.auth_key
    return res

def get_tables(host, port, auth_key, tables):
    try:
        conn = r.connect(host, port, auth_key=auth_key)
    except r.RqlDriverError as ex:
        raise RuntimeError(ex.message)

    dbs = r.db_list().run(conn)
    res = []

    if len(tables) == 0:
        tables = [[db] for db in dbs]

    for db_table in tables:
        if db_table[0] not in dbs:
            raise RuntimeError("Error: Database '%s' not found" % db_table[0])

        if len(db_table) == 1: # This is just a db name
            res.extend([(db_table[0], table) for table in r.db(db_table[0]).table_list().run(conn)])
        else: # This is db and table name
            if db_table[1] not in r.db(db_table[0]).table_list().run(conn):
                raise RuntimeError("Error: Table not found: '%s.%s'" % tuple(db_table))
            res.append(tuple(db_table))

    # Remove duplicates by making results a set
    return set(res)

def prepare_directories(base_path, db_table_set):
    db_set = set([db for (db, table) in db_table_set])
    for db in db_set:
        os.makedirs(base_path + "/%s" % db)

def write_table_metadata(conn, db, table, base_path):
    out = open(base_path + "/%s/%s.info" % (db, table), "w")
    table_info = r.db(db).table(table).info().run(conn)
    out.write(json.dumps(table_info) + "\n")
    out.close()

def read_table_into_queue(conn, db, table, task_queue, progress_info, exit_event):
    read_rows = 0
    for row in r.db(db).table(table).run(conn, time_format="raw"):
        if exit_event.is_set():
            break
        task_queue.put([row])

        # Update the progress every 20 rows - to reduce locking overhead
        read_rows += 1
        if read_rows % 20 == 0:
            progress_info[0].value += 20
    progress_info[0].value += read_rows % 20

def json_writer(filename, fields, task_queue, error_queue):
    try:
        with open(filename, "w") as out:
            first = True
            out.write("[")
            while True:
                item = task_queue.get()
                if len(item) != 1:
                    break
                row = item[0]

                if fields is not None:
                    for item in list(row.iterkeys()):
                        if item not in fields:
                            del row[item]
                if first:
                    first = False
                    out.write("\n" + json.dumps(row))
                else:
                    out.write(",\n" + json.dumps(row))
            out.write("\n]\n")
    except:
        ex_type, ex_class, tb = sys.exc_info()
        error_queue.put((ex_type, ex_class, traceback.extract_tb(tb)))

def csv_writer(filename, fields, task_queue, error_queue):
    try:
        with open(filename, "w") as out:
            out_writer = csv.writer(out)
            out_writer.writerow([s.encode('utf-8') for s in fields])

            while True:
                item = task_queue.get()
                if len(item) != 1:
                    break
                row = item[0]
                info = []
                # If the data is a simple type, just write it directly, otherwise, write it as json
                for field in fields:
                    if field not in row:
                        info.append(None)
                    elif isinstance(row[field], (int, long, float, complex)):
                        info.append(str(row[field]).encode('utf-8'))
                    elif isinstance(row[field], (str, unicode)):
                        info.append(row[field].encode('utf-8'))
                    else:
                        info.append(json.dumps(row[field]))
                out_writer.writerow(info)
    except:
        ex_type, ex_class, tb = sys.exc_info()
        error_queue.put((ex_type, ex_class, traceback.extract_tb(tb)))

def export_table(host, port, auth_key, db, table, directory, fields, format, error_queue, progress_info, exit_event):
    task_queue = multiprocessing.queues.SimpleQueue()

    if format == "json":
        filename = directory + "/%s/%s.json" % (db, table)
        writer = multiprocessing.Process(target=json_writer,
                                         args=(filename, fields, task_queue, error_queue))
    elif format == "csv":
        filename = directory + "/%s/%s.csv" % (db, table)
        writer = multiprocessing.Process(target=csv_writer,
                                         args=(filename, fields, task_queue, error_queue))
    else:
        error_queue.put((RuntimeError, RuntimeError("unknown format type: %s" % format),
                         traceback.extract_tb(sys.exc_info()[2])))
        return

    try:
        conn = r.connect(host, port, auth_key=auth_key)
        table_size = r.db(db).table(table).count().run(conn)
        progress_info[1].value = table_size
        write_table_metadata(conn, db, table, directory)
        writer.start()
        read_table_into_queue(conn, db, table, task_queue, progress_info, exit_event)
    except (r.RqlClientError, r.RqlDriverError) as ex:
        error_queue.put((RuntimeError, RuntimeError(ex.message), traceback.extract_tb(sys.exc_info()[2])))
    except:
        ex_type, ex_class, tb = sys.exc_info()
        error_queue.put((ex_type, ex_class, traceback.extract_tb(tb)))
    finally:
        if writer.is_alive():
            task_queue.put(("exit", "event")) # Exit is triggered by sending a message with two objects
            writer.join()
        else:
            error_queue.put((RuntimeError, RuntimeError("writer unexpectedly stopped"),
                             traceback.extract_tb(sys.exc_info()[2])))

def abort_export(signum, frame, exit_event, interrupt_event):
    interrupt_event.set()
    exit_event.set()

def print_progress(ratio):
    total_width = 40
    done_width = int(ratio * total_width)
    undone_width = total_width - done_width
    print "\r[%s%s] %3d%%" % ("=" * done_width, " " * undone_width, int(100 * ratio)),
    sys.stdout.flush()

def update_progress(progress_info):
    lowest_completion = 1.0
    for (current, max_count) in progress_info:
        curr_val = current.value
        max_val = max_count.value
        if curr_val < 0:
            lowest_completion = 0.0
        elif max_val <= 0:
            lowest_completion = 1.0
        else:
            lowest_completion = min(lowest_completion, float(curr_val) / max_val)

    print_progress(lowest_completion)

def run_clients(options, db_table_set):
    # Spawn one client for each db.table
    exit_event = multiprocessing.Event()
    processes = []
    error_queue = multiprocessing.queues.SimpleQueue()
    interrupt_event = multiprocessing.Event()

    signal.signal(signal.SIGINT, lambda a,b: abort_export(a, b, exit_event, interrupt_event))

    try:
        progress_info = [ ]

        for (db, table) in db_table_set:
            progress_info.append((multiprocessing.Value(ctypes.c_longlong, -1),
                                  multiprocessing.Value(ctypes.c_longlong, 0)))
            processes.append(multiprocessing.Process(target=export_table,
                                                     args=(options["host"],
                                                           options["port"],
                                                           options["auth_key"],
                                                           db, table,
                                                           options["directory"],
                                                           options["fields"],
                                                           options["format"],
                                                           error_queue,
                                                           progress_info[-1],
                                                           exit_event)))
            processes[-1].start()

        # Wait for all tables to finish
        while len(processes) > 0:
            time.sleep(0.1)
            if not error_queue.empty():
                exit_event.set() # Stop rather immediately if an error occurs
            processes = [process for process in processes if process.is_alive()]
            update_progress(progress_info)

        # If we were successful, make sure 100% progress is reported
        # (rows could have been deleted which would result in being done at less than 100%)
        if error_queue.empty() and not interrupt_event.is_set():
            print_progress(1.0)

        # Continue past the progress output line
        print ""
    finally:
        signal.signal(signal.SIGINT, signal.SIG_DFL)

    if interrupt_event.is_set():
        raise RuntimeError("Interrupted")

    if not error_queue.empty():
        # multiprocessing queues don't handling tracebacks, so they've already been stringified in the queue
        while not error_queue.empty():
            error = error_queue.get()
            print >> sys.stderr, "Traceback: %s" % (error[2])
            print >> sys.stderr, "%s: %s" % (error[0].__name__, error[1])
            raise RuntimeError("Errors occurred during export")

def main():
    try:
        options = parse_options()
    except RuntimeError as ex:
        print >> sys.stderr, "Usage:\n%s" % usage
        print >> sys.stderr, ex
        return 1

    try:
        db_table_set = get_tables(options["host"], options["port"], options["auth_key"], options["tables"])
        del options["tables"] # This is not needed anymore, db_table_set is more useful
        prepare_directories(options["directory"], db_table_set)
        start_time = time.time()
        run_clients(options, db_table_set)
    except RuntimeError as ex:
        print >> sys.stderr, ex
        return 1
    print "  Done (%d seconds)" % (time.time() - start_time)
    return 0

if __name__ == "__main__":
    exit(main())
