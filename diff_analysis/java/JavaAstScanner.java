import com.sun.source.tree.*;
import com.sun.source.util.*;
import javax.tools.*;
import javax.lang.model.element.*;
import javax.lang.model.type.*;
import javax.lang.model.util.Elements;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;
import java.util.regex.*;

/** Java 8 compatible AST scanner. Parse only; never compile business code. */
public final class JavaAstScanner {
    private static String json(String value) {
        if (value == null) return "null";
        StringBuilder b = new StringBuilder("\"");
        for (char c : value.toCharArray()) {
            if (c == '"' || c == '\\') b.append('\\').append(c);
            else if (c == '\n') b.append("\\n");
            else if (c == '\r') b.append("\\r");
            else if (c == '\t') b.append("\\t");
            else if (c < 32) b.append(String.format("\\u%04x", (int)c));
            else b.append(c);
        }
        return b.append('"').toString();
    }

    static String jsonValue(Object value) {
        if (value == null) return "null";
        if (value instanceof Number || value instanceof Boolean) return value.toString();
        if (value instanceof Map) {
            List<String> items = new ArrayList<String>();
            for (Object item : ((Map<?, ?>)value).entrySet()) {
                Map.Entry<?, ?> e = (Map.Entry<?, ?>)item;
                items.add(json(e.getKey().toString()) + ":" + jsonValue(e.getValue()));
            }
            return "{" + String.join(",", items) + "}";
        }
        if (value instanceof Iterable) {
            List<String> items = new ArrayList<String>();
            for (Object item : (Iterable<?>)value) items.add(jsonValue(item));
            return "[" + String.join(",", items) + "]";
        }
        return json(value.toString());
    }

    static void emit(String kind, Map<String, ?> fields, Map<String, Integer> nums) {
        StringBuilder b = new StringBuilder("{\"kind\":").append(json(kind));
        for (Map.Entry<String, ?> e : fields.entrySet())
            b.append(',').append(json(e.getKey())).append(':').append(jsonValue(e.getValue()));
        for (Map.Entry<String, Integer> e : nums.entrySet())
            b.append(',').append(json(e.getKey())).append(':').append(e.getValue());
        System.out.println(b.append('}').toString());
    }

    static Map<String, String> fields(String... values) {
        Map<String, String> m = new LinkedHashMap<String, String>();
        for (int i = 0; i < values.length; i += 2) m.put(values[i], values[i + 1]);
        return m;
    }

    static Map<String, Integer> ints(String k, int v, String k2, int v2) {
        Map<String, Integer> m = new LinkedHashMap<String, Integer>();
        m.put(k, v); m.put(k2, v2);
        return m;
    }

    /** Canonical javac method identity; only attributed non-error types qualify. */
    private static String bindingId(ExecutableElement executable, Elements elements) {
        Element parent = executable.getEnclosingElement();
        if (!(parent instanceof TypeElement)) return "";
        String owner = elements.getBinaryName((TypeElement)parent).toString();
        if (owner.isEmpty() || parent.asType().getKind() == TypeKind.ERROR) return "";
        List<String> params = new ArrayList<String>();
        for (VariableElement param : executable.getParameters()) {
            TypeMirror type = param.asType();
            if (type.getKind() == TypeKind.ERROR || type.getKind() == TypeKind.NONE
                    || type.toString().contains("<error>")) return "";
            params.add(type.toString());
        }
        return owner + "#" + executable.getSimpleName().toString()
            + "(" + String.join(",", params) + ")";
    }

    private static final class Source extends SimpleJavaFileObject {
        final String text;
        Source(Path file, String text) {
            super(file.toUri(), Kind.SOURCE);
            this.text = text;
        }
        @Override public CharSequence getCharContent(boolean ignored) { return text; }
    }

    private static final class Scanner extends TreePathScanner<Void, Void> {
        final CompilationUnitTree unit;
        final SourcePositions positions;
        final Trees trees;
        final Elements elements;
        final String path;
        final String pkg;
        final String scope;
        final Deque<String> classStack = new ArrayDeque<String>();
        final Deque<Map<String, String>> fieldTypes = new ArrayDeque<Map<String, String>>();
        Map<String, String> localTypes = new HashMap<String, String>();
        String owner = null;

        Scanner(CompilationUnitTree unit, Trees trees, Elements elements, String path, String scope) {
            this.unit = unit;
            this.positions = trees.getSourcePositions();
            this.trees = trees;
            this.elements = elements;
            this.path = path;
            this.scope = scope;
            this.pkg = unit.getPackageName() == null ? "" : unit.getPackageName().toString();
        }

        int line(long offset) {
            return offset < 0 ? 0 : (int)unit.getLineMap().getLineNumber(offset);
        }

        String clazz() {
            List<String> names = new ArrayList<String>(classStack);
            Collections.reverse(names);
            return (pkg.isEmpty() ? "" : pkg + ".") + String.join("$", names);
        }

        @Override public Void visitClass(ClassTree tree, Void unused) {
            if (tree.getSimpleName().length() == 0) return super.visitClass(tree, unused);
            classStack.push(tree.getSimpleName().toString());
            Map<String, String> types = new HashMap<String, String>();
            for (Tree member : tree.getMembers()) if (member instanceof VariableTree) {
                VariableTree f = (VariableTree)member;
                types.put(f.getName().toString(), f.getType() == null ? "" : f.getType().toString());
            }
            fieldTypes.push(types);
            super.visitClass(tree, unused);
            fieldTypes.pop();
            classStack.pop();
            return null;
        }

        @Override public Void visitMethod(MethodTree tree, Void unused) {
            if (classStack.isEmpty()) return super.visitMethod(tree, unused);
            StringBuilder signature = new StringBuilder(tree.getName().toString()).append('(');
            int count = 0;
            Map<String, String> scoped = new HashMap<String, String>(fieldTypes.peek());
            for (VariableTree p : tree.getParameters()) {
                if (count++ > 0) signature.append(',');
                signature.append(p.getType() == null ? "?" : p.getType().toString());
                scoped.put(p.getName().toString(), p.getType() == null ? "" : p.getType().toString());
            }
            signature.append(')');
            String previousOwner = owner;
            Map<String, String> previousTypes = localTypes;
            owner = clazz() + "#" + signature.toString();
            localTypes = scoped;
            long start = positions.getStartPosition(unit, tree);
            long end = positions.getEndPosition(unit, tree);
            Map<String, String> attrs = fields("path", path, "symbol", owner, "class", clazz(),
                "name", tree.getName().toString(), "signature", signature.toString(),
                "arity", String.valueOf(count));
            Element methodElement = trees.getElement(getCurrentPath());
            String methodBinding = methodElement instanceof ExecutableElement
                ? bindingId((ExecutableElement)methodElement, elements) : "";
            attrs.put("binding_id", methodBinding);
            attrs.put("source_scope", scope);
            attrs.put("binding_status", methodBinding.isEmpty()
                ? "ATTRIBUTION_UNAVAILABLE" : "JAVAC_ATTRIBUTED");
            Map<String, Object> methodFacts = new LinkedHashMap<String, Object>(attrs);
            methodFacts.put("behavior", new BehaviorFacts(unit, trees, elements).method(tree));
            emit("method", methodFacts, ints("start", line(start), "end", line(Math.max(start, end - 1))));
            super.visitMethod(tree, unused);
            owner = previousOwner;
            localTypes = previousTypes;
            return null;
        }

        @Override public Void visitVariable(VariableTree tree, Void unused) {
            if (owner != null && tree.getType() != null)
                localTypes.put(tree.getName().toString(), tree.getType().toString());
            return super.visitVariable(tree, unused);
        }

        @Override public Void visitMethodInvocation(MethodInvocationTree tree, Void unused) {
            if (owner != null) {
                ExpressionTree select = tree.getMethodSelect();
                String receiver = "";
                String name = select.toString();
                if (select instanceof MemberSelectTree) {
                    MemberSelectTree m = (MemberSelectTree)select;
                    name = m.getIdentifier().toString();
                    receiver = m.getExpression().toString();
                }
                String type = receiver.equals("this") || receiver.equals("super") ? clazz() : localTypes.get(receiver);
                if (type == null && receiver.length() > 0 && Character.isUpperCase(receiver.charAt(0)))
                    type = receiver;
                Element resolved = trees.getElement(getCurrentPath());
                String targetId = resolved instanceof ExecutableElement
                    ? bindingId((ExecutableElement)resolved, elements) : "";
                Map<String, String> attrs = fields(
                    "path", path, "owner", owner, "name", name,
                    "source_scope", scope,
                    "receiver", receiver, "receiver_type", type == null ? "" : type,
                    "target_binding_id", targetId,
                    "binding_status", targetId.isEmpty()
                        ? "ATTRIBUTION_UNAVAILABLE" : "JAVAC_ATTRIBUTED");
                emit("call", attrs, ints("arity", tree.getArguments().size(), "line",
                                        line(positions.getStartPosition(unit, tree))));
            }
            return super.visitMethodInvocation(tree, unused);
        }
    }

    private static Path sourceRoot(Path root, Path file, String source) {
        String relative = root.relativize(file).toString().replace('\\', '/');
        Matcher standard = Pattern.compile("^((?:.*?/)?src/(?:main|test)/java)(?:/|$)").matcher(relative);
        if (standard.find()) return root.resolve(standard.group(1));
        Matcher pkg = Pattern.compile("(?m)^\\s*package\\s+([\\w.]+)\\s*;").matcher(source);
        Path parent = file.getParent();
        if (pkg.find()) {
            String suffix = pkg.group(1).replace('.', '/');
            String folder = root.relativize(parent).toString().replace('\\', '/');
            if (folder.equals(suffix) || folder.endsWith("/" + suffix)) {
                for (String ignored : suffix.split("/")) parent = parent.getParent();
                return parent;
            }
        }
        // 非标准布局按最近的模块目录隔离，保持单文件包声明样例可用。
        for (Path p = file.getParent(); p != null && p.startsWith(root); p = p.getParent())
            if (Files.isRegularFile(p.resolve("pom.xml"))) return p;
        return root;
    }

    public static void main(String[] args) throws Exception {
        if (args.length != 1 && args.length != 2) {
            System.err.println("Usage: JavaAstScanner <source-root> [explicit-local-classpath]");
            System.exit(2);
        }
        final Path root = Paths.get(args[0]).toAbsolutePath().normalize();
        JavaCompiler compiler = ToolProvider.getSystemJavaCompiler();
        if (compiler == null) throw new IllegalStateException("JDK required (not JRE)");
        Map<Path, List<Source>> scopes = new TreeMap<Path, List<Source>>();
        try (java.util.stream.Stream<Path> stream = Files.walk(root)) {
            for (Path file : (Iterable<Path>)stream.filter(
                    p -> Files.isRegularFile(p) && p.toString().endsWith(".java")).sorted()::iterator) {
                String source = new String(Files.readAllBytes(file), StandardCharsets.UTF_8);
                Path scope = sourceRoot(root, file, source);
                if (!scopes.containsKey(scope)) scopes.put(scope, new ArrayList<Source>());
                scopes.get(scope).add(new Source(file, source));
            }
        }
        for (Map.Entry<Path, List<Source>> group : scopes.entrySet()) {
                String rel = root.relativize(group.getKey()).toString().replace('\\', '/');
                try {
                    StandardJavaFileManager manager = compiler.getStandardFileManager(
                        null, null, StandardCharsets.UTF_8);
                    try {
                        DiagnosticCollector<JavaFileObject> diagnostics =
                            new DiagnosticCollector<JavaFileObject>();
                        List<Source> sources = new ArrayList<Source>(group.getValue());
                        Set<java.net.URI> primary = new HashSet<java.net.URI>();
                        for (Source s : group.getValue()) primary.add(s.toUri());
                        if (rel.endsWith("src/test/java")) {
                            Path main = group.getKey().getParent().getParent().resolve("main/java");
                            if (scopes.containsKey(main)) sources.addAll(scopes.get(main));
                        }
                        List<String> options = new ArrayList<String>(Arrays.asList(
                            "-proc:none", "-implicit:none", "-sourcepath",
                            group.getKey().toString(), "-classpath",
                            args.length == 2 ? args[1] : root.resolve(".no-implicit-classpath").toString()));
                        JavacTask task = (JavacTask)compiler.getTask(new StringWriter(),
                            manager, diagnostics, options, null,
                            sources);
                        Iterable<? extends CompilationUnitTree> units = task.parse();
                        try {
                            // Symbol attribution only. No generate(), no project build,
                            // no annotation processors or script execution.
                            task.analyze();
                        } catch (RuntimeException attributionFailure) {
                            emit("error", fields("path", rel, "error", "JAVAC_ANALYZE_FAILED"),
                                 new LinkedHashMap<String, Integer>());
                        }
                        Trees trees = Trees.instance(task);
                        for (CompilationUnitTree unit : units) if (primary.contains(unit.getSourceFile().toUri())) {
                            String path = root.relativize(Paths.get(unit.getSourceFile().toUri())).toString().replace('\\', '/');
                            new Scanner(unit, trees, task.getElements(), path, rel).scan(unit, null);
                        }
                        int reported = 0;
                        int omitted = 0;
                        for (Diagnostic<? extends JavaFileObject> problem :
                                diagnostics.getDiagnostics()) {
                            if (problem.getKind() != Diagnostic.Kind.ERROR) continue;
                            if (reported >= 20) { omitted++; continue; }
                            String detail = problem.getMessage(Locale.ROOT);
                            if (detail.length() > 300) detail = detail.substring(0, 300);
                            String problemPath = problem.getSource() == null ? rel :
                                root.relativize(Paths.get(problem.getSource().toUri())).toString().replace('\\', '/');
                            emit("error", fields("path", problemPath,
                                 "error", "JAVAC_ATTRIBUTION_INCOMPLETE: " + detail),
                                 ints("line", (int)Math.max(0, problem.getLineNumber()),
                                      "column", (int)Math.max(0, problem.getColumnNumber())));
                            reported++;
                        }
                        if (omitted > 0)
                            emit("error", fields("path", rel,
                                 "error", "JAVAC_ATTRIBUTION_ERRORS_OMITTED"),
                                 ints("omitted", omitted, "shown", reported));
                    } finally {
                        manager.close();
                    }
                } catch (Exception ex) {
                    emit("error", fields("path", rel, "error",
                        ex.getClass().getSimpleName() + ": " + ex.getMessage()),
                        new LinkedHashMap<String, Integer>());
                }
        }
    }
}
